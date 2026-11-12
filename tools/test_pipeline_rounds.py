"""分析流水线轮次协议与调度前置判定的回归测试（标准库 unittest，无外部依赖）。

覆盖需求点：
1. 草稿不能直接归档（跳级拦截），动作只能顺序推进，倒序被拒；
2. 失败后恢复开启新轮次，迟到归档不能复活失败轮；
3. 终止与归档竞争只能落定一种结论，归档重放不能再造完成证据；
4. 每轮轨迹可追溯（events、证据 id、结论、轮次号）；
5. 调度启用必须核对关联节点【当前轮】完成证据，历史轮证据不解锁新触发；
6. 无轮次旧记录惰性兼容，不凭名称猜归属、不补造完成证据；
7. 动作两种提交位置（顶层 / values）都能取到。
"""
from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "/workspace/backend")

from app.schemas import EntryPayload, extract_action  # noqa: E402
from app.seed import SEED_ROWS  # noqa: E402
from app.services.pipeline import (  # noqa: E402
    ACTION_RULES,
    DONE_STATUS,
    FAILED_STATUS,
    RUNNING_STATUS,
    PipelineService,
)
from app.services.scheduler import SchedulerService  # noqa: E402
from app.store import store  # noqa: E402


def reset_store() -> None:
    """每个用例前把内存仓库恢复成种子数据。"""
    store._tables = {  # noqa: SLF001
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }


class PipelineRoundProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.svc = PipelineService()
        self.entry, missing = self.svc.create_entry(
            {"节点编号": "PIPE-T1", "节点类型": "ETL", "输入源": "s3://in"}
        )
        self.assertFalse(missing)
        self.eid = int(self.entry["id"])

    def test_01_happy_path_round_one(self) -> None:
        for action in ["提交调度", "启动运行", "归档结果"]:
            entry, msg = self.svc.run_action(self.eid, action)
            self.assertIsNotNone(entry, msg)
        entry = self.svc.get_entry(self.eid)
        self.assertEqual(entry["status"], DONE_STATUS)
        self.assertEqual(entry["current_round"], 1)
        r1 = entry["rounds"][0]
        self.assertEqual(r1["conclusion"], "完成")
        self.assertTrue(r1["completion_proof"].startswith(f"PROOF-{self.eid:04d}-R01"))
        self.assertFalse(entry["pending"])
        # 轨迹：登记 + 三个成功动作
        self.assertTrue(all(ev["ok"] for ev in r1["events"]))
        self.assertEqual([ev["action"] for ev in r1["events"]][-3:],
                         ["提交调度", "启动运行", "归档结果"])

    def test_02_no_skip_draft_cannot_archive(self) -> None:
        entry, msg = self.svc.run_action(self.eid, "归档结果")
        self.assertIsNone(entry)
        self.assertIn("只能按顺序推进", msg)
        # 跳级尝试被记录为失败裁决，状态未变、无完成证据
        fresh = self.svc.get_entry(self.eid)
        self.assertEqual(fresh["status"], "草稿")
        self.assertIsNone(fresh["rounds"][0]["completion_proof"])
        self.assertFalse(fresh["rounds"][0]["events"][-1]["ok"])

    def test_03_no_skip_draft_cannot_start(self) -> None:
        entry, msg = self.svc.run_action(self.eid, "启动运行")
        self.assertIsNone(entry)
        self.assertIn("待调度", msg)

    def test_04_no_reverse_after_done(self) -> None:
        for action in ["提交调度", "启动运行", "归档结果"]:
            self.svc.run_action(self.eid, action)
        # 已完成后倒序/重放动作全部被拒（提交调度是唯一合法出口：另起新轮）
        for action in ["启动运行", "终止运行", "归档结果"]:
            entry, msg = self.svc.run_action(self.eid, action)
            self.assertIsNone(entry, f"{action} 不应在已完成后生效")
            self.assertIn("终结", msg)
        fresh = self.svc.get_entry(self.eid)
        # 归档重放没有再造第二份证据
        proofs = [ev.get("completion_proof") for ev in fresh["rounds"][0]["events"]
                  if ev.get("completion_proof")]
        self.assertEqual(len(proofs), 1)
        # 提交调度开启第 2 轮，历史轮证据不携带到新轮
        entry, msg = self.svc.run_action(self.eid, "提交调度")
        self.assertIsNotNone(entry, msg)
        self.assertEqual(entry["current_round"], 2)
        self.assertEqual(entry["status"], "待调度")
        self.assertIsNone(entry["rounds"][1]["completion_proof"])
        self.assertEqual(entry["rounds"][0]["conclusion"], "完成")
        # 新轮仍须按顺序推进：待调度不能直接归档
        blocked, bmsg = self.svc.run_action(self.eid, "归档结果")
        self.assertIsNone(blocked)
        self.assertIn("只能按顺序推进", bmsg)

    def test_05_terminate_then_late_archive_rejected(self) -> None:
        self.svc.run_action(self.eid, "提交调度")
        self.svc.run_action(self.eid, "启动运行")
        entry, _ = self.svc.run_action(self.eid, "终止运行")
        self.assertEqual(entry["status"], FAILED_STATUS)
        self.assertTrue(entry["abnormal"])
        # 迟到归档不能复活失败轮
        late, msg = self.svc.run_action(self.eid, "归档结果")
        self.assertIsNone(late)
        self.assertIn("迟到的归档不能复活", msg)
        fresh = self.svc.get_entry(self.eid)
        self.assertEqual(fresh["status"], FAILED_STATUS)
        self.assertEqual(len(fresh["rounds"]), 1)
        self.assertIsNone(fresh["rounds"][0]["completion_proof"])
        self.assertEqual(fresh["rounds"][0]["conclusion"], "失败")

    def test_06_archive_then_terminate_rejected_single_conclusion(self) -> None:
        self.svc.run_action(self.eid, "提交调度")
        self.svc.run_action(self.eid, "启动运行")
        self.svc.run_action(self.eid, "归档结果")
        # 终止与归档竞争：归档已落定，终止不能再改写结论
        late, msg = self.svc.run_action(self.eid, "终止运行")
        self.assertIsNone(late)
        fresh = self.svc.get_entry(self.eid)
        self.assertEqual(fresh["status"], DONE_STATUS)
        self.assertEqual(fresh["rounds"][0]["conclusion"], "完成")

    def test_07_recovery_opens_new_round_and_old_round_keeps_failure(self) -> None:
        self.svc.run_action(self.eid, "提交调度")
        self.svc.run_action(self.eid, "启动运行")
        self.svc.run_action(self.eid, "终止运行")
        # 失败恢复：启动运行直接开新轮
        entry, msg = self.svc.run_action(self.eid, "启动运行")
        self.assertIsNotNone(entry, msg)
        self.assertEqual(entry["current_round"], 2)
        self.assertEqual(entry["status"], RUNNING_STATUS)
        self.assertFalse(entry["abnormal"])
        rounds = entry["rounds"]
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[0]["conclusion"], "失败")
        self.assertEqual(rounds[1]["state"], RUNNING_STATUS)
        self.assertIsNone(rounds[1]["completion_proof"])
        # 新轮里迟到的、针对旧轮语义的归档不成立：当前轮运行中可以归档（正常路径）
        entry, _ = self.svc.run_action(self.eid, "归档结果")
        self.assertEqual(entry["status"], DONE_STATUS)
        self.assertTrue(rounds[1]["completion_proof"].endswith("R02"))
        # 旧轮依旧是失败，没被复活
        self.assertEqual(rounds[0]["conclusion"], "失败")
        self.assertIsNone(rounds[0]["completion_proof"])

    def test_08_trace_is_traceable(self) -> None:
        self.svc.run_action(self.eid, "提交调度")
        bad, _ = self.svc.run_action(self.eid, "归档结果")  # 待调度状态跳级
        self.assertIsNone(bad)
        self.svc.run_action(self.eid, "启动运行")
        entry = self.svc.get_entry(self.eid)
        events = entry["rounds"][0]["events"]
        event_ids = [ev["event_id"] for ev in events]
        self.assertEqual(len(event_ids), len(set(event_ids)), "事件 id 必须唯一")
        self.assertTrue(all("at" in ev and "action" in ev and "ok" in ev for ev in events))
        blocked = [ev for ev in events if not ev["ok"]]
        self.assertEqual(len(blocked), 1)
        self.assertIn("只能按顺序推进", blocked[0]["reason"])

    def test_09_unknown_and_empty_action_rejected(self) -> None:
        entry, msg = self.svc.run_action(self.eid, "随便跑")
        self.assertIsNone(entry)
        self.assertIn("不属于", msg)
        entry, msg = self.svc.run_action(self.eid, "")
        self.assertIsNone(entry)

    def test_10_legacy_record_without_rounds(self) -> None:
        # 手工构造一条无 rounds 的旧记录
        rows = store.rows("pipeline")
        legacy_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": legacy_id,
            "status": "运行中",
            "pending": True,
            "abnormal": False,
            "节点编号": "PIPE-LEGACY",
        })
        entry = self.svc.get_entry(legacy_id)
        self.assertEqual(len(entry["rounds"]), 1)
        r1 = entry["rounds"][0]
        self.assertTrue(r1["legacy"])
        self.assertEqual(r1["state"], "运行中")
        self.assertIsNone(r1["completion_proof"], "旧记录不得补造完成证据")
        # 旧记录可以沿正常协议继续推进
        ok, msg = self.svc.run_action(legacy_id, "归档结果")
        self.assertIsNotNone(ok, msg)
        self.assertEqual(ok["rounds"][0]["conclusion"], "完成")

    def test_11_legacy_done_record_keeps_no_proof_and_blocks_scheduler(self) -> None:
        rows = store.rows("pipeline")
        legacy_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": legacy_id,
            "status": DONE_STATUS,
            "pending": False,
            "abnormal": False,
            "节点编号": "PIPE-OLD-DONE",
        })
        evidence, blocked = self.svc.current_completion_evidence(legacy_id)
        # 旧的已完成记录没有证据 id，不能作为调度前置凭证
        self.assertIsNone(evidence)
        self.assertIn("缺少本轮归档的完成证据", blocked)


class SchedulerPrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.pipe = PipelineService()
        self.sched = SchedulerService()
        entry, _ = self.pipe.create_entry(
            {"节点编号": "PIPE-S1", "节点类型": "ETL", "输入源": "s3://in"}
        )
        self.node_id = int(entry["id"])
        cfg, _ = self.sched.create_entry(
            {"配置编号": "SCHE-T1", "关联任务": "job", "调度表达式": "0 * * * *"}
        )
        self.cfg_id = int(cfg["id"])

    def _enable_flow(self) -> tuple[str, str]:
        """把配置推到待校验，返回启用结果。"""
        ok, msg = self.sched.run_action(self.cfg_id, "提交校验")
        self.assertIsNotNone(ok, msg)
        _, enable_msg = self.sched.run_action(self.cfg_id, "启用调度")
        return "", enable_msg

    def test_12_enable_without_binding_blocked(self) -> None:
        _, msg = self._enable_flow()
        self.assertIn("尚未绑定流水线节点", msg)
        cfg = self.sched.get_entry(self.cfg_id)
        self.assertEqual(cfg["status"], "待校验")
        self.assertFalse(cfg["前置裁决"]["ok"])

    def test_13_bind_rejects_name_guessing(self) -> None:
        entry, msg = self.sched.bind_node(self.cfg_id, "PIPE-S1")
        self.assertIsNone(entry)
        self.assertIn("数字 id", msg)
        entry, msg = self.sched.bind_node(self.cfg_id, 999999)
        self.assertIsNone(entry)
        self.assertIn("不存在", msg)

    def test_14_enable_requires_current_round_evidence(self) -> None:
        self.sched.bind_node(self.cfg_id, self.node_id)
        self.sched.run_action(self.cfg_id, "提交校验")
        # 节点还在草稿，启用被阻断并给出前置关系
        _, msg = self.sched.run_action(self.cfg_id, "启用调度")
        self.assertIn("缺少本轮归档的完成证据", msg)
        # 推进节点到归档完成
        for action in ["提交调度", "启动运行", "归档结果"]:
            self.pipe.run_action(self.node_id, action)
        ok, msg = self.sched.run_action(self.cfg_id, "启用调度")
        self.assertIsNotNone(ok, msg)
        self.assertEqual(ok["status"], "已启用")
        verdict = ok["前置裁决"]
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["round_no"], 1)
        self.assertTrue(verdict["completion_proof"])

    def test_15_previous_round_archive_does_not_unlock_new_trigger(self) -> None:
        # 第 1 轮完整归档
        for action in ["提交调度", "启动运行", "归档结果"]:
            self.pipe.run_action(self.node_id, action)
        self.sched.bind_node(self.cfg_id, self.node_id)
        self.sched.run_action(self.cfg_id, "提交校验")
        ok, _ = self.sched.run_action(self.cfg_id, "启用调度")
        self.assertIsNotNone(ok)
        # 节点开启第 2 轮（先提交调度进入待调度），历史证据不再有效
        self.sched.run_action(self.cfg_id, "停用调度")
        node, msg = self.pipe.run_action(self.node_id, "提交调度")
        self.assertIsNotNone(node, msg)
        self.assertEqual(node["current_round"], 2)
        _, blocked = self.pipe.current_completion_evidence(self.node_id)
        self.assertIn("上一轮归档不能解锁新触发", blocked)
        # 调度重新走流程后启用仍被阻断
        cfg2, _ = self.sched.create_entry(
            {"配置编号": "SCHE-T2", "关联任务": "job2", "调度表达式": "0 0 * * *"}
        )
        cid2 = int(cfg2["id"])
        self.sched.bind_node(cid2, self.node_id)
        self.sched.run_action(cid2, "提交校验")
        _, msg = self.sched.run_action(cid2, "启用调度")
        self.assertIn("上一轮归档不能解锁新触发", msg)
        # 第 2 轮归档后才放行
        self.pipe.run_action(self.node_id, "启动运行")
        self.pipe.run_action(self.node_id, "归档结果")
        ok, msg = self.sched.run_action(cid2, "启用调度")
        self.assertIsNotNone(ok, msg)
        self.assertEqual(ok["前置裁决"]["round_no"], 2)

    def test_16_failed_round_blocks_scheduler_until_recovered_and_archived(self) -> None:
        for action in ["提交调度", "启动运行", "终止运行"]:
            self.pipe.run_action(self.node_id, action)
        self.sched.bind_node(self.cfg_id, self.node_id)
        self.sched.run_action(self.cfg_id, "提交校验")
        _, msg = self.sched.run_action(self.cfg_id, "启用调度")
        self.assertIn("已判定失败", msg)
        # 恢复开新轮，未归档前仍阻断
        self.pipe.run_action(self.node_id, "启动运行")
        _, msg = self.sched.run_action(self.cfg_id, "启用调度")
        self.assertIn("缺少本轮归档的完成证据", msg)
        self.pipe.run_action(self.node_id, "归档结果")
        ok, msg = self.sched.run_action(self.cfg_id, "启用调度")
        self.assertIsNotNone(ok, msg)

    def test_17_scheduler_order_and_terminal_replay(self) -> None:
        # 草稿不能直接启用（跳级）
        entry, msg = self.sched.run_action(self.cfg_id, "启用调度")
        self.assertIsNone(entry)
        self.assertIn("只能按顺序推进", msg)
        self.sched.run_action(self.cfg_id, "提交校验")
        # 待校验不能停用（跳级）
        entry, msg = self.sched.run_action(self.cfg_id, "停用调度")
        self.assertIsNone(entry)
        # 停用后任何动作不能复活结论
        self.sched.bind_node(self.cfg_id, self.node_id)
        for action in ["提交调度", "启动运行", "归档结果"]:
            self.pipe.run_action(self.node_id, action)
        self.sched.run_action(self.cfg_id, "启用调度")
        self.sched.run_action(self.cfg_id, "停用调度")
        entry, msg = self.sched.run_action(self.cfg_id, "启用调度")
        self.assertIsNone(entry)
        self.assertIn("不能重复落定结论", msg)
        cfg = self.sched.get_entry(self.cfg_id)
        self.assertEqual(cfg["status"], "已停用")
        self.assertTrue(cfg["abnormal"])
        blocked_events = [ev for ev in cfg["events"] if not ev["ok"]]
        self.assertGreaterEqual(len(blocked_events), 2)


class PayloadCompatibilityTests(unittest.TestCase):
    def test_18_action_accepted_from_both_positions(self) -> None:
        p1 = EntryPayload(values={"action": "归档结果"})
        p2 = EntryPayload(action="归档结果")
        p3 = EntryPayload(values={}, action="启动运行")
        self.assertEqual(extract_action(p1), "归档结果")
        self.assertEqual(extract_action(p2), "归档结果")
        self.assertEqual(extract_action(p3), "启动运行")
        # values 优先
        p4 = EntryPayload(values={"action": "终止运行"}, action="归档结果")
        self.assertEqual(extract_action(p4), "终止运行")

    def test_19_action_rules_cover_full_sequence(self) -> None:
        self.assertEqual(list(ACTION_RULES), ["提交调度", "启动运行", "终止运行", "归档结果"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
