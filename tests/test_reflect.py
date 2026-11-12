"""rubric 评分与定向返工的单元测试。

重点验证三件事：
1. 分数确实等于“通过维度的权重之和”，不是拍脑袋给的；
2. 不达阈值时，缺陷被映射到正确的责任节点，而不是整链重跑；
3. 返工指令里带够执行所需的上下文（作业、节点、预算、依据）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.reflect import RUBRIC, Reflector  # noqa: E402
from gcs.simulator import RunMetrics  # noqa: E402
from gcs.verifier import Defect, VerifyReport  # noqa: E402

PACK = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")


def metrics(cost=2.0):
    return RunMetrics(jobs=10, sla_rate=1.0, budget_rate=1.0,
                      cost_per_gpu_hour=cost, avg_green_share=0.6)


def report_with(*defects):
    rep = VerifyReport(checked=10)
    rep.defects = list(defects)
    return rep


def sla_defect(owner, node, job_id="J001"):
    return Defect("L3-SLA 未达成", "L3", owner, node,
                  f"{job_id} 超出截止时间", "估计 60min / 实际 90min",
                  job_id, action="重排或重估")


class TestRubric(unittest.TestCase):
    def setUp(self):
        self.reflector = Reflector(PACK)

    def test_threshold_comes_from_knowledge_pack(self):
        self.assertEqual(self.reflector.threshold, int(PACK.rework_budget["pass_score"]))
        self.assertEqual(self.reflector.threshold, 80)

    def test_clean_report_scores_full_marks(self):
        decision = self.reflector.score(report_with(), metrics(), [], [], 1)
        self.assertEqual(decision.score, 100)
        self.assertTrue(decision.passed)
        self.assertEqual(len(decision.dims), len(RUBRIC))
        self.assertEqual(decision.directives, [])

    def test_score_equals_sum_of_passed_weights(self):
        decision = self.reflector.score(
            report_with(sla_defect("A3", "n_rework_schedule")), metrics(), [], [], 1)
        expected = sum(d.weight for d in decision.dims if d.passed)
        self.assertEqual(decision.score, expected)

    def test_hard_constraint_failure_drops_below_threshold(self):
        decision = self.reflector.score(
            report_with(sla_defect("A3", "n_rework_schedule")), metrics(), [], [], 1)
        self.assertEqual(decision.score, 100 - 30)
        self.assertFalse(decision.passed)
        failed = [d for d in decision.dims if not d.passed]
        self.assertEqual([d.name for d in failed], ["硬约束满足"])

    def test_cost_efficiency_is_advisory_only(self):
        # 单位成本 10 元/GPU·h 高于 6 元参考线：这一维不通过，但不阻塞、不触发返工
        decision = self.reflector.score(report_with(), metrics(cost=10.0), [], [], 1)
        self.assertEqual(decision.score, 95)
        self.assertTrue(decision.passed, "成本效率只是提示项，不应把总分拉下阈值")
        self.assertEqual(decision.directives, [])

    def test_low_confidence_notes_do_not_block(self):
        estimates = [{"job_id": "J001", "confidence": 0.3}]
        decision = self.reflector.score(report_with(), metrics(), [], estimates, 1)
        self.assertTrue(decision.passed)
        evidence_dim = [d for d in decision.dims if d.name == "证据强度"][0]
        self.assertIn("置信度低于 0.5", evidence_dim.reason)


class TestAttribution(unittest.TestCase):
    def setUp(self):
        self.reflector = Reflector(PACK)

    def test_sla_defect_targets_scheduler_node(self):
        decision = self.reflector.score(
            report_with(sla_defect("A3", "n_rework_schedule", "J007")),
            metrics(), [], [], 1)
        self.assertEqual(decision.target_nodes, ["n_rework_schedule"])
        self.assertEqual(decision.jobs_to_rework, ["J007"])
        self.assertEqual(decision.strategy, "reschedule_only")

    def test_profile_defect_triggers_reprofile_strategy(self):
        decision = self.reflector.score(
            report_with(sla_defect("A2", "n_rework_profile", "J009")),
            metrics(), [], [], 1)
        self.assertEqual(decision.target_nodes, ["n_rework_profile"])
        self.assertEqual(decision.strategy, "reprofile_and_reschedule")

    def test_directive_carries_actionable_context(self):
        decision = self.reflector.score(
            report_with(sla_defect("A3", "n_rework_schedule", "J003")),
            metrics(), [], [], 1)
        self.assertEqual(len(decision.directives), 1)
        d = decision.directives[0]
        for key in ("defect_id", "owner_agent", "target_node", "job_id",
                    "evidence", "action"):
            self.assertIn(key, d)
        self.assertEqual(d["owner_agent"], "A3")
        self.assertEqual(d["job_id"], "J003")
        self.assertTrue(d["defect_id"].startswith("CHK-"))

    def test_rework_command_carries_budget_and_scope(self):
        decision = self.reflector.score(
            report_with(sla_defect("A3", "n_rework_schedule")), metrics(), [], [], 1)
        cmd = decision.command
        self.assertEqual(cmd["round"], 1)
        self.assertEqual(cmd["target_nodes"], ["n_rework_schedule"])
        self.assertEqual(cmd["jobs"], ["J001"])
        self.assertIn("rounds", cmd["budget_remaining"])
        self.assertIn("tokens", cmd["budget_remaining"])
        self.assertIn("只重跑", cmd["note"])

    def test_only_blocking_defects_are_attributed(self):
        advisory = Defect("ADV-提示", "ADV", "A6", "n_reflect", "提示项", "n/a",
                          "J001", blocking=False)
        decision = self.reflector.score(
            report_with(advisory), metrics(), [], [], 1)
        self.assertTrue(decision.passed)
        self.assertEqual(decision.directives, [])


if __name__ == "__main__":
    unittest.main()
