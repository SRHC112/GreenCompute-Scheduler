# -*- coding: utf-8 -*-
"""trace_fit 阈值口径一致性测试：常量范围 + 出厂知识包在统一阈值下全部通过。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import trace_fit  # noqa: E402
from gcs.knowledge import KnowledgePack  # noqa: E402


class TestThresholdRanges(unittest.TestCase):
    def test_declared_ranges(self):
        th = trace_fit.THRESHOLDS
        self.assertIs(th["tariff_exact"], True)
        self.assertTrue(0.0 < th["ratio_rel_tol"] <= 0.10, th["ratio_rel_tol"])
        self.assertTrue(0.0 <= th["carbon_abs_tol"] <= 0.01, th["carbon_abs_tol"])
        self.assertTrue(0.80 <= th["green_corr_min"] <= 0.95, th["green_corr_min"])
        self.assertTrue(0.0 <= th["green_r2_info"] <= 1.0, th["green_r2_info"])


class TestShippedPackUnderUnifiedThresholds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")

    def test_all_four_checks_pass(self):
        checks = [trace_fit.check_tariff(self.pack), trace_fit.check_ratio(self.pack),
                  trace_fit.check_carbon(self.pack), trace_fit.check_green(self.pack)]
        for c in checks:
            self.assertTrue(c["passed"], f"{c['name']} 未通过：{c}")

    def test_green_uses_corr_as_gate_and_r2_as_info(self):
        g = trace_fit.check_green(self.pack)
        self.assertIn("r2_info_ok", g)          # R² 作为提示项存在
        self.assertIsInstance(g["passed"], bool)  # 硬门槛仍由 corr 决定


if __name__ == "__main__":
    unittest.main()
