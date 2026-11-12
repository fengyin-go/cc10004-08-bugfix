"""调度管理业务规则：状态流转、字段校验与跨模块前置判定。

与分析流水线的衔接
------------------
调度配置启用前必须显式绑定一个流水线节点（``关联节点`` 存节点 id，禁止按名称猜归属），
启用时核对该节点【当前执行轮次】的完成证据：

* 没有绑定节点 -> 阻断，提示先绑定；
* 绑定节点当前轮未归档完成 -> 阻断，并说明是缺证据、失败还是只有历史轮证据；
* 上一轮归档不能解锁新触发：只有当前轮持有完成证据才放行。

调度自身同样按顺序推进：草稿 -> 待校验 -> 已启用 -> 已停用，不允许跳级/倒序；
停用是终结结论，停用重放不会再造启用结论。所有裁决（含被阻断的尝试）都进 events 轨迹。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.services.pipeline import PipelineService
from app.store import store

MODULE = "scheduler"
REQUIRED_FIELDS = ["配置编号", "关联任务", "调度表达式"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES: dict[str, tuple[str, str]] = {
    "提交校验": ("草稿", "待校验"),
    "启用调度": ("待校验", "已启用"),
    "停用调度": ("已启用", "已停用"),
}
BIND_ACTION = "绑定节点"
NEGATIVE_ACTIONS = ["停用调度"]

_event_seq_lock = threading.Lock()
_event_seq = 0


def _next_event_id() -> str:
    global _event_seq
    with _event_seq_lock:
        _event_seq += 1
        return f"SCH-EVT-{_event_seq:06d}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


class SchedulerService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pipeline = PipelineService()

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
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

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
            entry["关联节点"] = None
            entry["前置裁决"] = None
            entry["events"] = [_make_event("登记配置", ok=True, to_state=STATUS_ORDER[0])]
            rows.append(entry)
        return entry, []

    def bind_node(self, entry_id: int, raw_node_id: Any) -> tuple[dict[str, Any] | None, str]:
        """显式绑定关联流水线节点；只认节点 id，不按名称猜归属。"""
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"调度配置 {entry_id} 不存在或已归档"
            try:
                node_id = int(str(raw_node_id).strip())
            except (TypeError, ValueError):
                reason = "绑定节点需提交流水线节点的数字 id，不支持按名称猜测归属"
                entry.setdefault("events", []).append(_make_event(BIND_ACTION, ok=False, reason=reason))
                return None, reason
            node = store.find("pipeline", node_id)
            if node is None:
                reason = f"流水线节点 {node_id} 不存在，绑定未生效"
                entry.setdefault("events", []).append(
                    _make_event(BIND_ACTION, ok=False, reason=reason, node_id=node_id)
                )
                return None, reason
            entry["关联节点"] = node_id
            entry["前置裁决"] = None
            entry.setdefault("events", []).append(
                _make_event(BIND_ACTION, ok=True, node_id=node_id, node_code=node.get("节点编号"))
            )
            return entry, f"调度配置已绑定流水线节点 {node_id}（{node.get('节点编号')}）"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"调度配置 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于调度管理可执行范围"

            entry.setdefault("events", [])
            current_state = str(entry.get("status") or STATUS_ORDER[0])
            expected_state, target = ACTION_RULES[action]

            if current_state == STATUS_ORDER[-1]:
                reason = f"调度配置已停用并终结，动作「{action}」不能重复落定结论"
                entry["events"].append(
                    _make_event(action, ok=False, reason=reason, from_state=current_state)
                )
                return None, reason

            if current_state != expected_state:
                reason = (
                    f"动作「{action}」要求配置处于「{expected_state}」，"
                    f"当前为「{current_state}」，调度动作只能按顺序推进"
                )
                entry["events"].append(
                    _make_event(action, ok=False, reason=reason, from_state=current_state)
                )
                return None, reason

            # 启用调度的跨模块前置判定：核对关联节点本轮完成证据。
            if action == "启用调度":
                node_id = entry.get("关联节点")
                if not node_id:
                    reason = "启用被阻断：调度配置尚未绑定流水线节点，请先通过绑定节点建立显式关联"
                    entry["events"].append(
                        _make_event(action, ok=False, reason=reason, from_state=current_state)
                    )
                    entry["前置裁决"] = {"ok": False, "reason": reason, "at": _now()}
                    return None, reason
                evidence, blocked = self._pipeline.current_completion_evidence(int(node_id))
                if blocked:
                    reason = f"启用被阻断：{blocked}"
                    entry["events"].append(
                        _make_event(
                            action,
                            ok=False,
                            reason=reason,
                            from_state=current_state,
                            node_id=int(node_id),
                        )
                    )
                    entry["前置裁决"] = {
                        "ok": False,
                        "reason": blocked,
                        "node_id": int(node_id),
                        "at": _now(),
                    }
                    return None, reason
                entry["前置裁决"] = {
                    "ok": True,
                    "reason": "",
                    "node_id": evidence["node_id"],
                    "round_no": evidence["round_no"],
                    "completion_proof": evidence["completion_proof"],
                    "at": _now(),
                }
                entry["events"].append(
                    _make_event(
                        action,
                        ok=True,
                        from_state=current_state,
                        to_state=target,
                        node_id=evidence["node_id"],
                        round_no=evidence["round_no"],
                        completion_proof=evidence["completion_proof"],
                    )
                )
            else:
                entry["events"].append(
                    _make_event(action, ok=True, from_state=current_state, to_state=target)
                )

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"调度配置已{action}"
