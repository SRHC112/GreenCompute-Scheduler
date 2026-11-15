# -*- coding: utf-8 -*-
"""知识包回归测试（2026-11-15）：布尔值拒绝、旧包缺新键友好诊断、权威包回归。

覆盖：
- 布尔值被拒：pass_score: true / max_rounds: true 各自抛出含「布尔」的中文错误；
- 旧版知识包（无 schema_version）缺新键 → 错误消息含「缺少」+ 具体键名 + 可操作提示，且不是裸 KeyError；
- 现有权威包 knowledge/ningxia_cluster.yaml 仍能正常加载并通过 validate（回归保护）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from gcs.knowledge import KnowledgePack

ROOT = pathlib.Path(__file__).resolve().parent.parent
GS = ", ".join(["0.5"] * 24)  # 24 点绿电占比

CORE_YAML = [
    "version: x",
    "region: r",
    f"green_share_by_hour: [{GS}]",
    "grid_carbon_factor: 0.6",
    "tariff: {valley_hours: [0], flat_hours: [1], peak_hours: [2]}",
    "pools: [{id: a, city: c, gpu: g, gpus: 4, pue: 1.2, gpu_power_kw: 0.4,"
    " amort_per_gpu_hour: 1.0, price: {valley: 0.3, flat: 0.5, peak: 0.9}}]",
    "job_profiles: {train: {gpu_hours: [6, 36], gpus: [8, 16], sla_slack: 1.2,"
    " budget_per_gpu_hour: 3.0, weights: {wait: 0.5, cost: 0.5}}}",
    "constraints: {min_green_share: 0.4, max_budget_overrun: 0.15}",
    "rework_budget: {max_rounds: 2, pass_score: 80, max_tokens: 600000, max_seconds: 900}",
]


def _constraints(omit) -> str:
    return "{min_green_share: 0.4, max_budget_overrun: 0.15}"


def _rework_budget(keep) -> str:
    return ", ".join(f"{k}: 1" for k in keep)


def _write(tmp: pathlib.Path, *, omit=(), bool_keys=(), schema_version=None) -> pathlib.Path:
    """构造一份最小知识包 YAML 后写入临时文件。

    omit: 移除必填键名（用于模拟旧包缺新键）；
    bool_keys: 值为 true 的键名（模拟布尔值传入）；
    schema_version: 显式声明版本，默认无声明（视为 v1 老包）。
    """
    lines = [ln for ln in CORE_YAML if not ln.startswith(("constraints:", "rework_budget:"))]
    if schema_version is not None:
        lines.insert(0, f"schema_version: {schema_version}")
    # 约束/预算段用占位，按 omit / bool_keys 后续替换
    lines += [
        "constraints: PLACEHOLDER_CONSTRAINTS",
        "rework_budget: PLACEHOLDER_REWORK",
    ]
    rework_keys = ("max_rounds", "pass_score", "max_tokens", "max_seconds")
    kept = [k for k in rework_keys if k not in omit]
    text = "\n".join(lines)
    # 按占位符替换（占位符只占值域，段名前缀已是行的一部分）；
    # 避免替换后出现「constraints: constraints:」导致 YAML 解析失败。
    text = text.replace("PLACEHOLDER_CONSTRAINTS", _constraints(omit))
    text = text.replace("PLACEHOLDER_REWORK", "{" + _rework_budget(kept) + "}")
    # 把「值为 1」的必填键写成 true，模拟布尔值传入
    for key in bool_keys:
        text = text.replace(f"{key}: 1", f"{key}: true")
    path = tmp / "p.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestKnowledgeBoolRejection(unittest.TestCase):
    """K2：布尔值必须在数值转换阶段被拒绝。"""

    def test_bool_pass_score_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(pathlib.Path(tmp), bool_keys=("pass_score",))
            try:
                KnowledgePack.load(p)
            except ValueError as exc:
                msg = str(exc)
                self.assertIn("pass_score", msg)
                self.assertIn("布尔", msg)  # 中文错误，必须含「布尔」
                self.assertNotIsInstance(exc, KeyError)  # 不是裸 KeyError
            else:
                self.fail("pass_score: true 应被拒绝")

    def test_bool_max_rounds_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(pathlib.Path(tmp), bool_keys=("max_rounds",))
            try:
                KnowledgePack.load(p)
            except ValueError as exc:
                msg = str(exc)
                self.assertIn("max_rounds", msg)
                self.assertIn("布尔", msg)
                self.assertNotIsInstance(exc, KeyError)
            else:
                self.fail("max_rounds: true 应被拒绝")


class TestKnowledgeOldPackFriendlyFailure(unittest.TestCase):
    """K1：旧版知识包（无 schema_version）缺新键应给出「缺少」+ 可操作提示。"""

    def test_old_pack_missing_new_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(pathlib.Path(tmp), omit=("max_tokens", "max_seconds"))
            # 无 schema_version → 按 v1 处理；v2 新增键缺失应被诊断
            try:
                KnowledgePack.load(p)
            except ValueError as exc:
                msg = str(exc)
                self.assertIn("缺少", msg)  # 必填键缺失要有「缺少」二字
                self.assertIn("max_tokens", msg)
                self.assertIn("max_seconds", msg)
                self.assertIn("rework_budget", msg)  # 指明所在段
                self.assertIn("schema_version", msg)  # 提示版本过低
                # 非裸 KeyError
                self.assertNotIsInstance(exc, KeyError)
            else:
                self.fail("旧包缺 v2 新增键应被诊断")

    def test_old_pack_missing_v1_base_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            # constraints 整个段缺失（老包写法）→ 顶层必填字段诊断
            lines = [ln for ln in CORE_YAML if not ln.startswith("constraints")]
            p = pathlib.Path(tmp) / "old.yaml"
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                KnowledgePack.load(p)
            except ValueError as exc:
                msg = str(exc)
                self.assertNotIsInstance(exc, KeyError)  # 不是裸 KeyError
                self.assertIn("缺少", msg)  # 有「缺少」而非裸抛出
                self.assertIn("补齐", msg)  # 可操作提示
                self.assertIn("constraints", msg)
            else:
                self.fail("缺 constraints 段应被诊断")


class TestKnowledgeAuthoritativePackRegression(unittest.TestCase):
    """K1 回归保护：现有权威包 knowledge/ningxia_cluster.yaml 仍正常。"""

    def test_ningxia_cluster_loads_and_validates(self):
        p = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
        self.assertEqual(p.schema_version, 1)  # 权威包缺 schema_version，按 v1 处理
        p.validate()  # 必须通过，不允许带病运行
        self.assertEqual(p.fingerprint(), "nx-2026.12/3pools/3profiles")


if __name__ == "__main__":
    unittest.main()
