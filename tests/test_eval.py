# -*- coding: utf-8 -*-
"""评测脚本（evaluation/run_eval.py）最小可用单元测试。

覆盖纯函数：并行映射保序、aggregate 均值/标准差、diff_lines 头部渲染。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import run_eval  # noqa: E402


class TestParallelMap(unittest.TestCase):
    def test_preserves_order_and_serial_equiv(self):
        items = list(range(30))
        fn = lambda x: x * 3 + 1  # noqa: E731
        expect = [fn(x) for x in items]
        self.assertEqual(run_eval.parallel_map_ordered(fn, items, workers=1), expect)
        self.assertEqual(run_eval.parallel_map_ordered(fn, items, workers=4), expect)


class TestAggregate(unittest.TestCase):
    def test_mean_and_sd_on_single_group(self):
        code = run_eval.GROUPS[0][0]
        fields = ("sla_rate", "budget_rate", "green_ok_rate", "avg_wait_min",
                  "p95_wait_min", "cost_per_gpu_hour", "avg_green_share",
                  "green_window_ratio", "total_carbon_kg", "rubric_score", "rounds")
        rows = [{"group": code, **{f: 2.0 for f in fields}} for _ in range(3)]
        out = run_eval.aggregate(rows)
        self.assertIn(code, out)
        self.assertEqual(out[code]["n"], 3)
        self.assertAlmostEqual(out[code]["sla_rate_mean"], 2.0)
        self.assertEqual(out[code]["sla_rate_sd"], 0.0)


class TestDiffLines(unittest.TestCase):
    def test_empty_diff_returns_header(self):
        lines = run_eval.diff_lines({})
        self.assertTrue(lines)
        self.assertTrue(any("置信区间" in x for x in lines))


class TestRunAllBootstrapChain(unittest.TestCase):
    def test_run_all_then_bootstrap_chain(self):
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            rows = run_eval.run_all(tmp, jobs_n=6, workers=1)
            self.assertEqual(len(rows), len(run_eval.GROUPS) * len(run_eval.SEEDS_FIXED))
            codes = {r["group"] for r in rows}
            self.assertEqual(codes, {g[0] for g in run_eval.GROUPS})
            for r in rows:
                for k in ("sla_rate","cost_per_gpu_hour","green_window_ratio","rubric_score","rounds"):
                    self.assertIn(k, r)
            ci = run_eval.bootstrap_ci(rows, n_boot=20, seed=7)
            self.assertEqual(set(ci.keys()), codes)
            for d in ci.values():
                self.assertLessEqual(d["sla_rate_lo95"], d["sla_rate_hi95"])
            diff = run_eval.bootstrap_diff(rows, n_boot=20, seed=7)
            self.assertTrue(diff)
            self.assertTrue(run_eval.diff_lines(diff))
            # CLI 守卫：--jobs 0 必须早退（rc=2 + 中文提示），
            # 且不得把既有 results.csv 覆盖成「行数正常但数值全 0」的半成品
            import subprocess
            sentinel = tmp / "results.csv"
            sentinel.write_text("group,seed\nG-A,1\n", encoding="utf-8")
            before = sentinel.read_bytes()
            proc = subprocess.run(
                [sys.executable, str(ROOT / "evaluation" / "run_eval.py"),
                 "--jobs", "0", "--outdir", str(tmp)],
                capture_output=True, encoding="utf-8", errors="replace", timeout=120)
            # 显式 encoding 而非 text=True：Windows runner 的 locale 是 cp1252，
            # 按 locale 解码子进程的 UTF-8 输出会得到乱码，断言必然失败
            # （2026-11-15 GitHub Windows runner 实测）
            self.assertEqual(proc.returncode, 2, "--jobs 0 应以退出码 2 早退")
            self.assertIn("--jobs 必须 >= 1", proc.stderr)
            self.assertEqual(sentinel.read_bytes(), before,
                             "参数非法时不得改写既有产物")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRunParetoSmoke(unittest.TestCase):
    def test_runs_and_shape(self):
        from gcs.knowledge import KnowledgePack
        pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
        out = run_eval.run_pareto(pack, jobs_n=6, seeds=[7], workers=1)
        self.assertEqual(out["jobs"], 6)
        rows = out["rows"]
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["wait_scale"] for r in rows], [0.5, 1.0, 2.0, 4.0, 8.0, 16.0])
        for r in rows:
            for k in ("avg_wait_min", "cost_per_gpu_hour", "green_window_ratio", "sla_rate"):
                self.assertIn(k, r)


class TestRunSensitivitySmoke(unittest.TestCase):
    def test_runs_and_shape(self):
        from gcs.knowledge import KnowledgePack
        pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
        out = run_eval.run_sensitivity(pack, jobs_n=6, seeds=[7], workers=1)
        self.assertEqual(out["jobs"], 6)
        self.assertEqual([r["threshold"] for r in out["threshold"]], [60, 70, 80, 90, 95])
        self.assertEqual(len(out["weights"]), 4)
        for r in out["threshold"]:
            for k in ("rounds", "final_rubric", "sla_rate", "cost_per_gpu_hour", "green_window_ratio"):
                self.assertIn(k, r)
        for r in out["weights"]:
            self.assertIn("green_share_target", r)
            self.assertIn("green_share_actual", r)


class TestAggregateCorrectness(unittest.TestCase):
    """aggregate 统计正确性（独立于 write_html 显示契约）：用可手算样本核对 mean/sd/ci95。"""

    def test_mean_sd_ci_match_hand_values(self):
        import math
        code, name = run_eval.GROUPS[0][0], run_eval.GROUPS[0][1]
        fields = ("sla_rate", "budget_rate", "green_ok_rate", "avg_wait_min", "p95_wait_min",
                  "cost_per_gpu_hour", "avg_green_share", "green_window_ratio",
                  "total_carbon_kg", "rubric_score", "rounds")
        rows = [{"group": code, "group_name": name, "seed": i, "jobs": 3,
                 **{f: v for f in fields}} for i, v in enumerate((1.0, 3.0, 5.0))]
        out = run_eval.aggregate(rows)[code]
        sd = math.sqrt(8.0 / 3.0)                    # pstdev([1,3,5])
        self.assertAlmostEqual(out["sla_rate_mean"], 3.0)
        self.assertAlmostEqual(out["sla_rate_sd"], sd, places=9)
        self.assertAlmostEqual(out["sla_rate_ci95"], 1.96 * sd / math.sqrt(3), places=9)


if __name__ == "__main__":
    unittest.main()
