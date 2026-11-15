# -*- coding: utf-8 -*-
"""run_eval 输出层（write_*）单元测试：正常写入 / 空数据 / 边界值 / 异常路径。

风格与 tests/test_eval.py 一致：unittest + 临时目录，断言落盘结构与关键内容。
覆盖 4 个函数：write_csv / write_summary_json / write_summary / write_html。
其中 write_html 的「相对 G-A」差异表做逐指标数值精度校验（期望比值 / 精度 / 容差）。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import run_eval  # noqa: E402

_FIELDS11 = ("sla_rate", "budget_rate", "green_ok_rate", "avg_wait_min", "p95_wait_min",
             "cost_per_gpu_hour", "avg_green_share", "green_window_ratio",
             "total_carbon_kg", "rubric_score", "rounds")


def _six_group_rows():
    """构造 6 组 × 2 种子、11 项指标齐全的合成行（值非零，避免写看板时除零）。"""
    rows = []
    for code, name, _k, _d in run_eval.GROUPS:
        for seed in (7, 42):
            rows.append({"group": code, "group_name": name, "seed": seed, "jobs": 6,
                         **{f: 1.0 + (seed % 3) * 0.5 for f in _FIELDS11}})
    return rows


def _varying_rows():
    """各 (组, 指标) 取值互不相同，使 G-A 与 G-D 的比值随指标变化（用于比值精度校验）。"""
    rows = []
    for gi, (code, name, _k, _d) in enumerate(run_eval.GROUPS):
        for seed in (7, 42):
            vals = {f: round((gi + 1) * (mi + 2) * (1 + 0.1 * mi), 4)
                    for mi, f in enumerate(_FIELDS11)}
            rows.append({"group": code, "group_name": name, "seed": seed, "jobs": 6, **vals})
    return rows


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestWriteCsv(_TmpCase):
    def _row(self):
        return dict(zip(run_eval.FIELDS, ["v"] * len(run_eval.FIELDS)))

    def test_normal_write(self):
        rows = [self._row() for _ in range(3)]
        p = self.dir / "results.csv"
        run_eval.write_csv(rows, p)
        self.assertTrue(p.is_file())
        self.assertEqual(len(p.read_text(encoding="utf-8-sig").splitlines()), 1 + len(rows))

    def test_empty_rows_header_only(self):
        p = self.dir / "results.csv"
        run_eval.write_csv([], p)
        self.assertEqual(len(p.read_text(encoding="utf-8-sig").splitlines()), 1)

    def test_extra_key_raises(self):
        row = self._row()
        row["__unexpected__"] = "x"
        with self.assertRaises(ValueError):
            run_eval.write_csv([row], self.dir / "r.csv")

    def test_missing_parent_raises(self):
        with self.assertRaises(FileNotFoundError):
            run_eval.write_csv([], self.dir / "nope" / "r.csv")


class TestWriteSummaryJson(_TmpCase):
    def test_normal_write(self):
        p = self.dir / "summary.json"
        run_eval.write_summary_json(_six_group_rows(), p)
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {g[0] for g in run_eval.GROUPS})
        self.assertEqual(len(data[run_eval.GROUPS[0][0]]["sla_rate"]), 2)  # [mean, sd]

    def test_empty_rows_writes_empty_object(self):
        p = self.dir / "summary.json"
        run_eval.write_summary_json([], p)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {})

    def test_single_row_sd_zero(self):
        code, name = run_eval.GROUPS[0][0], run_eval.GROUPS[0][1]
        row = {"group": code, "group_name": name, "seed": 7, "jobs": 6,
               **{f: 2.0 for f in _FIELDS11}}
        p = self.dir / "summary.json"
        run_eval.write_summary_json([row], p)
        item = json.loads(p.read_text(encoding="utf-8"))[code]
        self.assertEqual(item["n"], 1)
        self.assertEqual(item["sla_rate"][1], 0.0)

    def test_missing_parent_raises(self):
        with self.assertRaises(FileNotFoundError):
            run_eval.write_summary_json([], self.dir / "nope" / "s.json")


class TestWriteSummary(_TmpCase):
    def test_normal_write(self):
        rows = _six_group_rows()
        p = self.dir / "summary.md"
        run_eval.write_summary(rows, run_eval.aggregate(rows), p)
        t = p.read_text(encoding="utf-8")
        self.assertTrue(t.startswith("# 对照实验结果摘要"))
        self.assertIn("G-A", t)
        self.assertIn("G-D", t)
        # 一致性更新：表头为完整指标定义、列数保持 11；CI 表仍为 4 列
        from gcs.metrics_meta import LABEL
        header = next(l for l in t.splitlines() if l.startswith("| 组别 |"))
        self.assertIn(LABEL["green_window_ratio"], header)
        self.assertEqual(len(header.split("|")) - 2, 11)
        diff = {"G-A_minus_G-D": {}}
        for field, _label, _fmt in run_eval.DIFF_METRICS:
            diff["G-A_minus_G-D"][field + "_lo95"] = 0.1
            diff["G-A_minus_G-D"][field + "_hi95"] = 0.2
        ci_header = next(l for l in run_eval.diff_lines(diff) if l.startswith("| 对比 |"))
        self.assertEqual(len(ci_header.split("|")) - 2, 4)

    def test_empty_stats_header_only(self):
        p = self.dir / "summary.md"
        run_eval.write_summary([], {}, p)
        t = p.read_text(encoding="utf-8")
        self.assertTrue(t.startswith("# 对照实验结果摘要"))
        self.assertNotIn("## 相对 G-A", t)  # 无 base/full 时不渲染对比段

    def test_missing_parent_raises(self):
        with self.assertRaises(FileNotFoundError):
            run_eval.write_summary([], {}, self.dir / "nope" / "s.md")


# write_html「相对 G-A」差异表用到的 8 个指标
_HTML_METRICS = ("sla_rate", "budget_rate", "avg_green_share", "cost_per_gpu_hour",
                 "green_window_ratio", "avg_wait_min", "total_carbon_kg", "rubric_score")


class TestWriteHtml(_TmpCase):
    def test_normal_write_and_delta_rendered(self):
        rows = _six_group_rows()
        p = self.dir / "eval_report.html"
        run_eval.write_html(rows, run_eval.aggregate(rows), p)
        t = p.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", t)
        self.assertIn("评测看板", t)
        self.assertIn(run_eval.GROUPS[0][0], t)
        # 「相对 G-A」差异行必须真实渲染（key+"_mean" 键匹配的回归锚点）
        self.assertIn("<td class='mono'>sla_rate</td>", t)
        # 一致性更新：HTML 表头取自统一术语字典且经 html.escape
        from html import escape
        from gcs.metrics_meta import LABEL
        self.assertIn(escape(LABEL["green_window_ratio"]), t)

    def test_empty_stats_no_crash(self):
        p = self.dir / "eval_report.html"
        run_eval.write_html([], {}, p)
        self.assertIn("<!DOCTYPE html>", p.read_text(encoding="utf-8"))

    def test_missing_parent_raises(self):
        with self.assertRaises(FileNotFoundError):
            run_eval.write_html([], {}, self.dir / "nope" / "e.html")


class TestWriteHtmlDeltaPrecision(_TmpCase):
    """差异表逐指标数值精度校验：期望比值 / 精度（小数位）/ 容差（半格 + 相对误差）。"""

    MEAN_DECIMALS = 2      # 基准/目标值显示 2 位小数
    PCT_DECIMALS = 1       # 比值显示 1 位小数（带显式符号）
    MEAN_HALF_ULP = 0.005  # 2 位小数的半格误差
    PCT_HALF_ULP = 0.05    # 1 位小数的半格误差
    REL_TOL = 1e-6         # 额外相对误差，吸收浮点表示误差

    def _render(self):
        rows = _varying_rows()
        stats = run_eval.aggregate(rows)
        p = self.dir / "eval_report.html"
        run_eval.write_html(rows, stats, p)
        return stats, p.read_text(encoding="utf-8")

    @staticmethod
    def _row_pattern(key):
        # 差异行： <td class='mono'>key</td><td>base → full</td><td>pct%</td>
        return (r"<td class='mono'>" + re.escape(key) +
                r"</td><td>([-0-9.]+) → ([-0-9.]+)</td><td>([-+0-9.]+)%</td>")

    def test_each_metric_ratio_precision(self):
        stats, t = self._render()
        base, full = stats["G-A"], stats["G-D"]
        for key in _HTML_METRICS:
            with self.subTest(metric=key):
                m = re.search(self._row_pattern(key), t)
                self.assertIsNotNone(m, f"差异表缺少指标 {key} 的行")
                b_disp, f_disp, pct_disp = m.group(1), m.group(2), m.group(3)
                exp_b, exp_f = base[f"{key}_mean"], full[f"{key}_mean"]
                exp_pct = (exp_f / exp_b - 1.0) * 100.0

                # 1) 精度/格式：小数位数与百分比符号
                self.assertRegex(
                    b_disp, rf"^-?\d+\.\d{{{self.MEAN_DECIMALS}}}$",
                    f"{key} 基准值应为 {self.MEAN_DECIMALS} 位小数，实际 {b_disp!r}")
                self.assertRegex(
                    f_disp, rf"^-?\d+\.\d{{{self.MEAN_DECIMALS}}}$",
                    f"{key} 目标值应为 {self.MEAN_DECIMALS} 位小数，实际 {f_disp!r}")
                self.assertRegex(
                    pct_disp, rf"^[+-]\d+\.\d{{{self.PCT_DECIMALS}}}$",
                    f"{key} 比值应为带符号 {self.PCT_DECIMALS} 位小数，实际 {pct_disp!r}")

                # 2) 数值：半格 + 相对误差容差
                self.assertAlmostEqual(
                    float(b_disp), exp_b,
                    delta=self.MEAN_HALF_ULP + self.REL_TOL * abs(exp_b),
                    msg=f"{key} 基准显示 {b_disp} 与期望 {exp_b:.6f} 超出容差")
                self.assertAlmostEqual(
                    float(f_disp), exp_f,
                    delta=self.MEAN_HALF_ULP + self.REL_TOL * abs(exp_f),
                    msg=f"{key} 目标显示 {f_disp} 与期望 {exp_f:.6f} 超出容差")
                self.assertAlmostEqual(
                    float(pct_disp), exp_pct,
                    delta=self.PCT_HALF_ULP + self.REL_TOL * abs(exp_pct),
                    msg=f"{key} 比值显示 {pct_disp}% 与期望 {exp_pct:+.3f}% 超出容差")

    def test_pct_independently_recomputed_from_display(self):
        """不依赖内部 stats，直接用差异表里显示的 base/full 复算 (full/base-1)*100 对照。"""
        _stats, t = self._render()
        for key in _HTML_METRICS:
            with self.subTest(metric=key):
                m = re.search(self._row_pattern(key), t)
                self.assertIsNotNone(m, f"缺少 {key} 行")
                b, f, pct = float(m.group(1)), float(m.group(2)), float(m.group(3))
                recomputed = (f / b - 1.0) * 100.0
                self.assertAlmostEqual(
                    pct, recomputed, delta=self.PCT_HALF_ULP + 1e-9,
                    msg=f"{key} 显示比值 {pct}% 与按显示值复算 {recomputed:.3f}% 不一致")


class TestWriteHtmlDeltaScope(_TmpCase):
    """验证「相对 G-A」差异表只含 G-A → G-D 一对（看板设计预期，不涉及其它组对）。"""

    def test_only_g_a_to_g_d_pair(self):
        rows = _varying_rows()
        stats = run_eval.aggregate(rows)
        p = self.dir / "eval_report.html"
        run_eval.write_html(rows, stats, p)
        t = p.read_text(encoding="utf-8")
        # 1) 对比头唯一，且确为 G-A → G-D
        self.assertEqual(t.count("G-A → G-D"), 1)
        # 2) 其它组对不应出现（看板只做 G-A → G-D 这一对）
        for other in ("G-B → G-D", "G-A → G-E1", "G-A → G-E2",
                      "G-D → G-E1", "G-D → G-E2", "G-B → G-A"):
            self.assertNotIn(other, t)
        # 3) 差异行恰为 8 个指标（无 <b> 的 mono 单元格），与 _HTML_METRICS 一致
        delta_cells = re.findall(r"<td class='mono'>(?!<b>)([a-z_]+)</td>", t)
        self.assertEqual(sorted(delta_cells), sorted(_HTML_METRICS))


if __name__ == "__main__":
    unittest.main()
