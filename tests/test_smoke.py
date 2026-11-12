"""冒烟测试：一条命令确认仓库可运行。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.agents import ROLE_CARDS, Profiler, Scheduler  # noqa: E402
from gcs.dag import build_default  # noqa: E402
from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.orchestrator import Orchestrator, Policy  # noqa: E402
from gcs.simulator import Cluster, make_jobs  # noqa: E402

PACK = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")


class TestKnowledge(unittest.TestCase):
    def test_pack_loads_and_validates(self):
        self.assertEqual(PACK.version, "nx-2026.12")
        self.assertEqual(len(PACK.green_share_by_hour), 24)
        self.assertEqual(len(PACK.pools), 3)

    def test_weights_sum_to_one(self):
        for name, prof in PACK.job_profiles.items():
            self.assertAlmostEqual(prof.weight_sum(), 1.0, places=6, msg=name)

    def test_fingerprint_is_stable(self):
        self.assertEqual(PACK.fingerprint(), PACK.fingerprint())


class TestSimulator(unittest.TestCase):
    def test_jobs_are_deterministic(self):
        a = make_jobs(PACK, n=25, seed=7)
        b = make_jobs(PACK, n=25, seed=7)
        self.assertEqual([(j.id, j.duration_actual, j.budget) for j in a],
                         [(j.id, j.duration_actual, j.budget) for j in b])

    def test_cluster_capacity_is_respected(self):
        cluster = Cluster(PACK)
        pool = PACK.pools[0]
        self.assertTrue(cluster.fits(pool.id, 0, 60, 4))
        cluster.place(pool.id, 0, 60, 4, "J001")
        self.assertFalse(cluster.fits(pool.id, 30, 60, pool.gpus))
        cluster.relocate_free({"J001"})
        self.assertTrue(cluster.fits(pool.id, 0, 60, 4))


class TestDag(unittest.TestCase):
    def test_levels_and_loop_edges(self):
        dag = build_default()
        self.assertIn("n_schedule", dag.nodes)
        loop_nodes = [n for n in dag.nodes.values() if n.loop]
        self.assertTrue(loop_nodes, "DAG 必须包含返工边")
        self.assertGreater(len(dag.levels()), 1)


class TestAgents(unittest.TestCase):
    def test_role_cards_cover_all_agents(self):
        for rid in ("A1", "A2", "A3", "A4", "A5", "A6", "A7"):
            self.assertIn(rid, ROLE_CARDS)
            self.assertTrue(ROLE_CARDS[rid].tools)

    def test_profiler_learn_updates_history(self):
        p = Profiler(PACK)
        jobs = make_jobs(PACK, n=6, seed=3)
        p.learn([], jobs)  # 空结果不应报错
        est = p.estimate(jobs)
        self.assertEqual(len(est), len(jobs))
        for e in est:
            self.assertIn("evidence", e)
            self.assertIn("confidence", e)

    def test_scheduler_plan_covers_all_jobs(self):
        jobs = make_jobs(PACK, n=12, seed=5)
        est = Profiler(PACK).estimate(jobs)
        plan = Scheduler(PACK).plan(jobs, est, Cluster(PACK))
        self.assertEqual(len(plan), len(jobs))
        for jid, p in plan.items():
            self.assertIn("pool_id", p)
            self.assertIn("start_min", p)
            self.assertTrue(p.get("rationale"), "决策必须带对照依据")


class TestOrchestrator(unittest.TestCase):
    def test_full_policy_converges(self):
        jobs = make_jobs(PACK, n=20, seed=20261111)
        art = Orchestrator(PACK, policy=Policy(name="G-D 本方案")).run(jobs)
        self.assertEqual(len(art.results), len(jobs))
        self.assertGreaterEqual(art.rounds, 1)
        self.assertTrue(art.events)
        self.assertIn("n_report", art.dag.nodes)

    def test_rule_baselines_run(self):
        jobs = make_jobs(PACK, n=15, seed=11)
        for rule in ("fcfs", "sjf"):
            art = Orchestrator(PACK, policy=Policy(name=rule, rule=rule, max_rounds=0)).run(jobs)
            self.assertEqual(len(art.results), len(jobs))

    def test_metrics_are_finite(self):
        jobs = make_jobs(PACK, n=15, seed=2026)
        art = Orchestrator(PACK, policy=Policy(name="G-D 本方案")).run(jobs)
        m = art.metrics.as_dict()
        for k, v in m.items():
            if isinstance(v, float):
                self.assertTrue(v == v and abs(v) != float("inf"), msg=k)


if __name__ == "__main__":
    unittest.main(verbosity=2)
