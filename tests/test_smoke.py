"""冒烟测试：一条命令确认仓库可运行。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.agents import ROLE_CARDS, Profiler, Scheduler  # noqa: E402
from gcs.dag import build_default  # noqa: E402
from gcs.knowledge import KnowledgePack, segment_of  # noqa: E402
from gcs.orchestrator import Orchestrator, Policy  # noqa: E402
from gcs.simulator import Cluster, make_jobs  # noqa: E402

PACK = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")


class TestKnowledge(unittest.TestCase):
    def test_pack_loads_and_validates(self):
        self.assertEqual(PACK.version, "nx-2026.12")
        self.assertEqual(len(PACK.green_share_by_hour), 24)
        self.assertEqual(len(PACK.pools), 3)
        # 分时电价：重叠必须在加载期拒绝；合法配置（含空档）行为不变
        from dataclasses import replace
        full = list(range(24))

        def expect_error(t):
            with self.assertRaises(ValueError) as ctx:
                replace(PACK, tariff=t).validate()
            return str(ctx.exception)

        msg = expect_error({"valley_hours": full, "flat_hours": full, "peak_hours": full})
        self.assertIn("重叠", msg)
        # 并集恰为 0-23 但部分重叠（旧实现只取并集会漏过）
        msg = expect_error({"valley_hours": list(range(13)),
                            "flat_hours": list(range(10, 24)), "peak_hours": []})
        self.assertIn("重叠", msg)
        self.assertIn("10", msg)
        for t in (
            {"valley_hours": [0, 0] + list(range(1, 24)), "flat_hours": [], "peak_hours": []},
            {"valley_hours": full + [24], "flat_hours": [], "peak_hours": []},
            {"valley_hours": full, "flat_hours": [], "peak_hours": [True]},
            {"valley_hours": full, "flat_hours": []},
        ):
            expect_error(t)
        legal = {"valley_hours": full, "flat_hours": [], "peak_hours": []}
        replace(PACK, tariff=legal).validate()
        replace(PACK, tariff=PACK.tariff).validate()
        # P3-1：CLI 友好入口把「缺文件 / YAML 语法错 / 缺键 / 空文件 / 纯标量」五类统一收敛为 KnowledgeError
        from gcs.knowledge import KnowledgeError, load_knowledge
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.yaml"
            with self.assertRaises(KnowledgeError) as ctx:
                load_knowledge(missing)
            self.assertIn("FileNotFoundError", str(ctx.exception))
            broken = Path(tmp) / "broken.yaml"
            broken.write_text("version: [unclosed\n", encoding="utf-8")
            with self.assertRaises(KnowledgeError) as ctx:
                load_knowledge(broken)
            self.assertIn("Error:", str(ctx.exception), "YAML 语法错误应带上原始异常类型")
            partial = Path(tmp) / "partial.yaml"
            partial.write_text("version: '1'\n", encoding="utf-8")
            with self.assertRaises(KnowledgeError):
                load_knowledge(partial)
            # 相邻区间不算重叠：离散小时下谷 0-6 与 平 7-9 相接是合法的
            replace(PACK, tariff={"valley_hours": list(range(0, 7)),
                                  "flat_hours": list(range(7, 10)),
                                  "peak_hours": list(range(10, 24))}).validate()
            # 跨零点合法：谷含 22,23,0,1 与 平 2..21 不构成重叠
            replace(PACK, tariff={"valley_hours": [22, 23, 0, 1],
                                  "flat_hours": list(range(2, 22)),
                                  "peak_hours": []}).validate()
            # segment_of 对 0-23 逐时唯一映射（重叠已在加载期被拒绝）
            t3 = {"valley_hours": list(range(0, 8)), "flat_hours": list(range(8, 16)),
                  "peak_hours": list(range(16, 24))}
            for h in range(24):
                want = "valley" if h < 8 else ("flat" if h < 16 else "peak")
                self.assertEqual(segment_of(h, t3), want, f"小时 {h} 的时段映射应唯一")
            # 空文件 / 纯标量：yaml 解析成功但结构不是映射，旧实现会抛 TypeError 裸 traceback
            empty = Path(tmp) / "empty.yaml"
            empty.write_text("", encoding="utf-8")
            with self.assertRaises(KnowledgeError) as ctx:
                load_knowledge(empty)
            # 结构前置断言：根节点不是映射时给字段级中文错误（不再靠 TypeError 兜底）
            self.assertIn("根节点必须是映射", str(ctx.exception))
            self.assertIn("NoneType", str(ctx.exception))
            scalar = Path(tmp) / "scalar.yaml"
            scalar.write_text("hello\n", encoding="utf-8")
            with self.assertRaises(KnowledgeError):
                load_knowledge(scalar)
            # 结构畸形：price 写成列表 → 字段级中文错误，而不是下游 AttributeError 裸栈
            bad_price = Path(tmp) / "bad_price.yaml"
            bad_price.write_text(
                "version: x\nregion: y\ngreen_share_by_hour: [0.5]\ngrid_carbon_factor: 0.6\n"
                "tariff: {valley_hours: [0], flat_hours: [1], peak_hours: [2]}\n"
                "pools: [{id: a, city: c, gpu: g, gpus: 4, pue: 1.2, gpu_power_kw: 0.4,"
                " amort_per_gpu_hour: 1.0, price: [1, 2, 3]}]\n"
                "job_profiles: {}\nconstraints: {}\nrework_budget: {}\n", encoding="utf-8")
            with self.assertRaises(KnowledgeError) as ctx:
                load_knowledge(bad_price)
            self.assertIn("pools[0].price 必须是映射", str(ctx.exception))
            # 结构畸形：gpu_hours 只有 1 个元素 → 明确要求长度为 2 的区间
            bad_profile = Path(tmp) / "bad_profile.yaml"
            bad_profile.write_text(
                "version: x\nregion: y\ngreen_share_by_hour: [0.5]\ngrid_carbon_factor: 0.6\n"
                "tariff: {valley_hours: [0], flat_hours: [1], peak_hours: [2]}\n"
                "pools: [{id: a, city: c, gpu: g, gpus: 4, pue: 1.2, gpu_power_kw: 0.4,"
                " amort_per_gpu_hour: 1.0, price: {valley: 0.3, flat: 0.5, peak: 0.9}}]\n"
                "job_profiles: {train: {gpu_hours: [6], gpus: [8, 16], sla_slack: 1.2,"
                " budget_per_gpu_hour: 3.0, weights: {wait: 1.0}}}\n"
                "constraints: {}\nrework_budget: {}\n", encoding="utf-8")
            with self.assertRaises(KnowledgeError) as ctx:
                load_knowledge(bad_profile)
            self.assertIn("gpu_hours 必须是长度为 2 的区间", str(ctx.exception))

    def test_weights_sum_to_one(self):
        for name, prof in PACK.job_profiles.items():
            self.assertAlmostEqual(prof.weight_sum(), 1.0, places=6, msg=name)

    def test_fingerprint_is_stable(self):
        import os, subprocess
        code = ("import sys; sys.path.insert(0, '.'); "
                "from gcs.knowledge import KnowledgePack; "
                "print(KnowledgePack.load('knowledge/ningxia_cluster.yaml').fingerprint())")
        out = subprocess.check_output([sys.executable, "-c", code], cwd=str(ROOT),
                                      env=dict(os.environ, PYTHONHASHSEED="12345")).decode().strip()
        self.assertEqual(PACK.fingerprint(), out)


class TestSimulator(unittest.TestCase):
    def test_jobs_are_deterministic(self):
        a = make_jobs(PACK, n=25, seed=7)
        b = make_jobs(PACK, n=25, seed=7)
        self.assertEqual([(j.id, j.duration_actual, j.budget) for j in a],
                         [(j.id, j.duration_actual, j.budget) for j in b])
        # 一致性更新：--jobs 下限校验，合法取值原样返回、非法取值给中文提示
        import argparse
        from gcs.demo import MIN_JOBS, positive_jobs
        self.assertEqual(MIN_JOBS, 1)
        self.assertEqual(positive_jobs("30"), 30)
        for raw in ("0", "-3", "abc"):
            with self.assertRaises(argparse.ArgumentTypeError):
                positive_jobs(raw)

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
