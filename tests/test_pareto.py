"""帕累托与权重缩放的单元测试。

关注三件事：
1. 权重缩放后各分项之和仍为 1（不能因为旋钮破坏目标函数的归一化）；
2. 排队权重放大确实让平均等待下降（旋钮真的作用在引擎上，而不是空转）；
3. 同一 seed 下运行结果可复现（反事实扫描的前提）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.orchestrator import Orchestrator, Policy  # noqa: E402
from gcs.simulator import make_jobs  # noqa: E402

PACK = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")


class TestWeightScaling(unittest.TestCase):
    def test_scaled_weights_keep_sum_one(self):
        """放大某个权重后，差额必须摊到其余分项，权重和保持为 1。"""
        w = dict(PACK.weights_for("train"))
        scaled = Orchestrator._scale_one(w, "wait", 4.0)
        self.assertAlmostEqual(sum(scaled.values()), 1.0, places=6)
        self.assertGreater(scaled["wait"], w["wait"])

    def test_scale_one_is_identity_at_one(self):
        w = dict(PACK.weights_for("infer"))
        self.assertEqual(Orchestrator._scale_one(w, "wait", 1.0), w)

    def test_scale_one_handles_degenerate_input(self):
        """目标分项为 0 时不改动，避免除零。"""
        w = {"wait": 0.0, "cost": 0.5, "green": 0.3, "carbon": 0.1, "reliability": 0.1}
        self.assertEqual(Orchestrator._scale_one(w, "wait", 8.0), w)


class TestWaitWeightEffect(unittest.TestCase):
    def test_higher_wait_weight_reduces_average_wait(self):
        """排队权重放大后，平均等待必须不增——否则这个旋钮是假的。"""
        jobs = make_jobs(PACK, n=40, seed=20261111)
        waits = []
        for k in (1.0, 8.0):
            art = Orchestrator(PACK, policy=Policy(name=f"wait×{k:g}", wait_scale=k)).run(jobs)
            waits.append(art.metrics.avg_wait_min)
        self.assertLessEqual(waits[1], waits[0],
                             f"排队权重 ×8 的等待 {waits[1]} 不应大于 ×1 的 {waits[0]}")

    def test_same_seed_is_reproducible(self):
        """反事实扫描要求同 seed 可复现，否则前沿不可信。"""
        jobs = make_jobs(PACK, n=20, seed=7)
        a = Orchestrator(PACK, policy=Policy(name="w", wait_scale=2.0)).run(jobs)
        b = Orchestrator(PACK, policy=Policy(name="w", wait_scale=2.0)).run(make_jobs(PACK, n=20, seed=7))
        self.assertEqual(a.metrics.cost_per_gpu_hour, b.metrics.cost_per_gpu_hour)
        self.assertEqual(a.metrics.avg_wait_min, b.metrics.avg_wait_min)


if __name__ == "__main__":
    unittest.main(verbosity=2)
