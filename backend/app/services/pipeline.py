"""分析流水线业务规则：按节点执行轮次（round）记录可追溯轨迹，强制动作顺序推进。

状态协议（提交边界）
--------------------
一个节点的生命周期由若干「执行轮次」组成，每轮只允许沿固定顺序推进：

    提交调度：草稿   -> 待调度
    启动运行：待调度 -> 运行中
    终止运行：运行中 -> 已失败（本轮终结，结论=失败）
    归档结果：运行中 -> 已完成（本轮终结，结论=完成，产出完成证据）

规则要点：
* 节点动作只能按顺序推进，不允许跳级（如草稿直接归档）或倒序（如已完成再启动）。
* 终止与归档竞争同一轮时只能落定一种结论：先落定者终结本轮，后来者被拒绝。
* 失败后恢复：对「已失败」节点执行「启动运行」会开启一个全新轮次，旧轮保留失败结论；
  迟到的归档落在失败轮上会被拒绝，不能复活已经终结的轮次。
* 已完成节点若要再跑，只能从「提交调度」开始新轮；上一轮的归档证据不能解锁新触发
  （跨模块的调度前置判定只认当前轮证据，见 ``current_completion_evidence``）。
* 没有轮次信息的旧记录在首次访问时惰性补建第 1 轮，按当前状态落位；旧记录不凭空
  生成完成证据，也不按名称猜测任何归属关系。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "pipeline"
REQUIRED_FIELDS = ["节点编号", "节点类型", "输入源"]
STATUS_ORDER = ["草稿", "待调度", "运行中", "已完成", "已失败"]

# 动作 -> （允许执行时节点所处状态, 目标状态）。顺序协议的唯一事实来源。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "提交调度": ("草稿", "待调度"),
    "启动运行": ("待调度", "运行中"),
    "终止运行": ("运行中", "已失败"),
    "归档结果": ("运行中", "已完成"),
}
# 失败恢复不属于顺序内的普通推进，单独声明：已失败 + 启动运行 => 开启新轮。
RECOVERY_ACTION = "启动运行"
FAILED_STATUS = "已失败"
DONE_STATUS = "已完成"
RUNNING_STATUS = "运行中"
TERMINAL_STATUSES = (DONE_STATUS, FAILED_STATUS)

# 事件序号在进程内单调递增，保证轨迹可排序、可引用。
_event_seq_lock = threading.Lock()
_event_seq = 0


def _next_event_id() -> str:
    global _event_seq
    with _event_seq_lock:
        _event_seq += 1
        return f"EVT-{_event_seq:06d}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _proof_id(entry_id: int, round_no: int) -> str:
    return f"PROOF-{entry_id:04d}-R{round_no:02d}"


def _make_event(action: str, *, ok: bool, reason: str = "", **extra: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": _next_event_id(),
        "at": _now(),
        "action": action,
        "ok": ok,
        "reason": reason,
    }
    event.update(extra)
    return event


def _ensure_rounds(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """给无轮次的旧记录惰性补建第 1 轮；不补造任何完成证据。"""
    rounds = entry.get("rounds")
    if isinstance(rounds, list) and rounds:
        return rounds

    status = str(entry.get("status") or STATUS_ORDER[0])
    round_one: dict[str, Any] = {
        "round_no": 1,
        "state": status,
        "opened_at": _now(),
        "closed_at": None,
        "conclusion": None,
        "completion_proof": None,
        "legacy": True,
        "events": [],
    }
    if status in TERMINAL_STATUSES:
        round_one["closed_at"] = _now()
        round_one["conclusion"] = "完成" if status == DONE_STATUS else "失败"
    entry["rounds"] = [round_one]
    entry["current_round"] = 1
    return entry["rounds"]


def _current_round(entry: dict[str, Any]) -> dict[str, Any]:
    rounds = _ensure_rounds(entry)
    return rounds[-1]


class PipelineService:
    def __init__(self) -> None:
        # 内存仓库无事务，用锁把「判定 + 落定」收进同一个提交边界，
        # 避免终止与归档竞争时两种结论同时落定。
        self._lock = threading.RLock()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("节点编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        with self._lock:
            _ensure_rounds(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with self._lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["current_round"] = 1
            entry["rounds"] = [
                {
                    "round_no": 1,
                    "state": STATUS_ORDER[0],
                    "opened_at": _now(),
                    "closed_at": None,
                    "conclusion": None,
                    "completion_proof": None,
                    "legacy": False,
                    "events": [_make_event("登记节点", ok=True, round_no=1, to_state=STATUS_ORDER[0])],
                }
            ]
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"流水线节点 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于分析流水线可执行范围"

            rounds = _ensure_rounds(entry)
            current = rounds[-1]
            current_state = str(current.get("state") or entry.get("status") or "")

            # 失败恢复：已失败节点的「启动运行」开启全新轮次，旧轮结论保持失败。
            if current_state == FAILED_STATUS and action == RECOVERY_ACTION:
                new_round = self._open_round(entry, reason=f"失败恢复：第 {current['round_no']} 轮终止后重启")
                entry["status"] = RUNNING_STATUS
                entry["pending"] = True
                entry["abnormal"] = False
                new_round["events"].append(
                    _make_event(
                        action,
                        ok=True,
                        round_no=new_round["round_no"],
                        from_state=FAILED_STATUS,
                        to_state=RUNNING_STATUS,
                        recovery_from_round=current["round_no"],
                    )
                )
                return entry, (
                    f"流水线节点第 {new_round['round_no']} 轮已启动运行"
                    f"（承接失败的第 {current['round_no']} 轮）"
                )

            # 已完成节点重跑：只能从「提交调度」开启全新轮次，旧轮完成证据保留在历史轮，
            # 不会被带到新轮；其它动作对已终结轮一律拒绝。
            if current_state == DONE_STATUS and action == "提交调度":
                new_round = self._open_round(
                    entry, reason=f"重跑开启：第 {current['round_no']} 轮归档完成后另起新轮"
                )
                new_round["state"] = "待调度"
                entry["status"] = "待调度"
                entry["pending"] = True
                entry["abnormal"] = False
                new_round["events"].append(
                    _make_event(
                        action,
                        ok=True,
                        round_no=new_round["round_no"],
                        from_state=DONE_STATUS,
                        to_state="待调度",
                        rerun_from_round=current["round_no"],
                    )
                )
                return entry, (
                    f"流水线节点已开启第 {new_round['round_no']} 轮并提交调度"
                    f"（第 {current['round_no']} 轮证据仅留存于历史轮）"
                )

            expected_state, target = ACTION_RULES[action]

            # 已终结轮次收到迟到动作：归档不能复活失败轮，任何重放都不能再造完成证据。
            if current_state in TERMINAL_STATUSES:
                reason = self._closed_reason(current, action)
                current["events"].append(
                    _make_event(
                        action,
                        ok=False,
                        reason=reason,
                        round_no=current["round_no"],
                        from_state=current_state,
                    )
                )
                return None, reason

            if current_state != expected_state:
                reason = (
                    f"动作「{action}」要求节点处于「{expected_state}」，"
                    f"当前第 {current['round_no']} 轮为「{current_state}」，节点动作只能按顺序推进"
                )
                current["events"].append(
                    _make_event(
                        action,
                        ok=False,
                        reason=reason,
                        round_no=current["round_no"],
                        from_state=current_state,
                    )
                )
                return None, reason

            # 顺序推进并落定结论。
            current["state"] = target
            entry["status"] = target
            entry["pending"] = target not in TERMINAL_STATUSES
            entry["abnormal"] = target == FAILED_STATUS

            event = _make_event(
                action,
                ok=True,
                round_no=current["round_no"],
                from_state=current_state,
                to_state=target,
            )
            if target in TERMINAL_STATUSES:
                current["closed_at"] = _now()
                if target == DONE_STATUS:
                    proof = _proof_id(entry_id, int(current["round_no"]))
                    current["conclusion"] = "完成"
                    current["completion_proof"] = proof
                    event["completion_proof"] = proof
                else:
                    current["conclusion"] = "失败"
            current["events"].append(event)
            return entry, f"流水线节点第 {current['round_no']} 轮已{action}"

    def _open_round(self, entry: dict[str, Any], *, reason: str) -> dict[str, Any]:
        rounds = _ensure_rounds(entry)
        new_no = int(rounds[-1]["round_no"]) + 1
        new_round: dict[str, Any] = {
            "round_no": new_no,
            "state": RUNNING_STATUS,
            "opened_at": _now(),
            "closed_at": None,
            "conclusion": None,
            "completion_proof": None,
            "legacy": False,
            "events": [_make_event("开启轮次", ok=True, round_no=new_no, reason=reason)],
        }
        rounds.append(new_round)
        entry["current_round"] = new_no
        return new_round

    @staticmethod
    def _closed_reason(closed_round: dict[str, Any], action: str) -> str:
        no = closed_round["round_no"]
        conclusion = closed_round.get("conclusion")
        if conclusion == "失败":
            if action == "归档结果":
                return (
                    f"第 {no} 轮已因终止运行判定为失败并终结，迟到的归档不能复活该轮；"
                    "如需恢复请对节点执行启动运行以开启新轮次"
                )
            return f"第 {no} 轮已终结（结论：失败），动作「{action}」不能重复落定结论"
        if conclusion == "完成":
            return (
                f"第 {no} 轮已归档完成并终结（证据：{closed_round.get('completion_proof')}），"
                "归档重放不能再造完成证据；如需重跑请先提交调度开启新轮次"
            )
        return f"第 {no} 轮已终结，动作「{action}」不被接受"

    def current_completion_evidence(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """跨模块前置判定：只核对关联节点【当前轮】的完成证据。

        返回 (证据信息, 阻断原因)。证据存在时原因为空；任何阻断都返回可读原因，
        并且不会因为历史轮次归档过就放行——上一轮归档不能解锁新触发。
        """
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"关联流水线节点 {entry_id} 不存在或已归档"
            rounds = _ensure_rounds(entry)
            current = rounds[-1]
            no = int(current["round_no"])
            proof = current.get("completion_proof")
            if current.get("state") == DONE_STATUS and proof:
                return (
                    {
                        "node_id": entry_id,
                        "node_code": entry.get("节点编号"),
                        "round_no": no,
                        "completion_proof": proof,
                        "closed_at": current.get("closed_at"),
                    },
                    "",
                )
            if current.get("conclusion") == "失败":
                return None, (
                    f"关联节点 {entry_id} 当前第 {no} 轮已判定失败，"
                    "须先恢复运行并归档本轮结果后才能触发调度"
                )
            older_done = [r for r in rounds[:-1] if r.get("completion_proof")]
            if older_done:
                last = older_done[-1]
                return None, (
                    f"关联节点 {entry_id} 的完成证据属于第 {last['round_no']} 轮，"
                    f"当前第 {no} 轮（{current.get('state')}）尚未归档；"
                    "上一轮归档不能解锁新触发"
                )
            return None, (
                f"关联节点 {entry_id} 当前第 {no} 轮状态为「{current.get('state')}」，"
                "缺少本轮归档的完成证据"
            )
