"""五层验证器的单元测试。

每一层都要能被单独触发：造一个该层该抓的缺陷，断言它被抓到，
并且带上正确的责任角色（owner）与责任节点（node）。
这是「定向返工」成立的前提——归因错了，返工就会退错节点。
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.orchestrator import Orchestrator, Policy  # noqa: E402
from gcs.simulator import make_jobs  # noqa: E402
from gcs.verifier import Verifier  # noqa: E402

PACK = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")


class TestVerifierLayers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 只跑一次编排，后续用例都在它的产物上做变形，避免重复计算
        cls.jobs = make_jobs(PACK, n=20, seed=20261111)
        cls.art = Orchestrator(PACK, llm=None, policy=Policy()).run(cls.jobs)
        cls.verifier = Verifier(PACK)  # 不传 llm，强制走离线规则分支

    def _verify(self, jobs=None, plan=None, results=None):
        return self.verifier.verify(
            jobs if jobs is not None else self.jobs,
            plan if plan is not None else self.art.plan,
            results if results is not None else self.art.results,
            self.art.metrics,
        )

    def _codes(self, report):
        return [d.code for d in report.defects]

    # ---------------- L1 结构 ----------------
    def test_clean_run_passes_structure_numbers_and_evidence(self):
        rep = self._verify()
        bad = [d for d in rep.blocking if d.layer in ("L1", "L2", "L4")]
        self.assertEqual(bad, [], f"正常跑完不应有结构/数值/依据缺陷：{[d.code for d in bad]}")

    def test_l1_detects_unscheduled_job(self):
        victim = self.jobs[0].id
        plan = {k: v for k, v in self.art.plan.items() if k != victim}
        rep = self._verify(plan=plan)
        hit = [d for d in rep.defects if d.code == "L1-未调度" and d.job_id == victim]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].owner, "A3")
        self.assertEqual(hit[0].node, "n_schedule")
        self.assertTrue(hit[0].action)

    def test_l1_detects_missing_result(self):
        victim = self.art.results[0].job_id
        results = [r for r in self.art.results if r.job_id != victim]
        rep = self._verify(results=results)
        hit = [d for d in rep.defects if d.code == "L1-结果缺失" and d.job_id == victim]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].owner, "A4")
        self.assertEqual(hit[0].node, "n_execute")

    # ---------------- L2 数值 ----------------
    def test_l2_detects_cost_that_cannot_be_recomputed(self):
        results = list(self.art.results)
        results[0] = replace(results[0], cost=results[0].cost + 100.0)
        rep = self._verify(results=results)
        hit = [d for d in rep.defects
               if d.code == "L2-成本不可复算" and d.job_id == results[0].job_id]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].owner, "A4")
        self.assertIn("复算", hit[0].detail)

    def test_l2_detects_illegal_duration(self):
        results = list(self.art.results)
        results[0] = replace(results[0], end_min=results[0].start_min)
        rep = self._verify(results=results)
        hit = [d for d in rep.defects if d.code == "L2-时长非法"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].owner, "A2")
        self.assertEqual(hit[0].node, "n_rework_profile")

    # ---------------- L3 约束与归因 ----------------
    def test_l3_blames_profiler_when_estimate_is_too_low(self):
        jobs = list(self.jobs)
        victim = jobs[0]
        # 实际时长是估计值的 3 倍：这是“估错了”，应退 A2
        jobs[0] = replace(victim, duration_est=max(1, victim.duration_actual // 3))
        results = [replace(r, sla_ok=False) if r.job_id == victim.id else r
                   for r in self.art.results]
        rep = self._verify(jobs=jobs, results=results)
        hit = [d for d in rep.defects if d.code == "L3-SLA 未达成"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].owner, "A2")
        self.assertEqual(hit[0].node, "n_rework_profile")

    def test_l3_blames_scheduler_when_estimate_is_accurate(self):
        jobs = list(self.jobs)
        victim = jobs[0]
        # 估计与真实一致却仍超时：这是“排错了”，应退 A3
        jobs[0] = replace(victim, duration_est=victim.duration_actual)
        results = [replace(r, sla_ok=False) if r.job_id == victim.id else r
                   for r in self.art.results]
        rep = self._verify(jobs=jobs, results=results)
        hit = [d for d in rep.defects if d.code == "L3-SLA 未达成"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].owner, "A3")
        self.assertEqual(hit[0].node, "n_rework_schedule")

    # ---------------- L4 可达性 ----------------
    def test_l4_detects_missing_evidence(self):
        victim = next(iter(self.art.plan))
        plan = dict(self.art.plan)
        plan[victim] = {k: v for k, v in plan[victim].items() if k != "rationale"}
        rep = self._verify(plan=plan)
        hit = [d for d in rep.defects if d.code.startswith("L4")]
        self.assertTrue(hit, "去掉决策依据后 L4 应当报缺陷")

    # ---------------- L5 语义 ----------------
    def test_l5_rejects_decision_without_any_exclusion_reason(self):
        victim = next(iter(self.art.plan))
        plan = dict(self.art.plan)
        plan[victim] = dict(plan[victim])
        plan[victim]["rationale"] = {
            "选定": "ZW-A100 @ 120min", "排除更便宜": "", "排除更绿": "",
        }
        rep = self._verify(plan=plan)
        hit = [d for d in rep.defects if d.code == "L5-依据不自洽"]
        self.assertEqual(len(hit), 1, "只说选了什么、不说排除了什么，L5 应判定不自洽")

    def test_l5_accepts_full_rationale(self):
        rep = self._verify()
        self.assertEqual([d for d in rep.defects if d.layer == "L5"], [])

    # ---------------- 缺陷契约 ----------------
    def test_every_defect_carries_owner_node_and_action(self):
        victim = self.jobs[0].id
        plan = {k: v for k, v in self.art.plan.items() if k != victim}
        results = [r for r in self.art.results if r.job_id != self.art.results[0].job_id]
        rep = self._verify(plan=plan, results=results)
        self.assertTrue(rep.defects)
        for d in rep.defects:
            self.assertTrue(d.owner, f"{d.code} 缺 owner")
            self.assertTrue(d.node, f"{d.code} 缺 node")
            self.assertTrue(d.evidence, f"{d.code} 缺 evidence")
            self.assertIn(d.layer, ("L1", "L2", "L3", "L4", "L5"))

    def test_report_summarises_defects_by_layer(self):
        plan = {k: v for k, v in self.art.plan.items() if k != self.jobs[0].id}
        rep = self._verify(plan=plan)
        by_layer = rep.by_layer()
        self.assertGreaterEqual(by_layer.get("L1", 0), 1)
        self.assertEqual(sum(by_layer.values()), len(rep.defects))


if __name__ == "__main__":
    unittest.main()
