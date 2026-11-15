# -*- coding: utf-8 -*-
"""安装顺序回归测试（2026-11-15 P0/P1 修复）。

背景：全新机器上系统 Python 通常没有 PyYAML。修复前 ``install.py`` 的知识包
语法预检会 ``import yaml`` 失败并被包成「知识包解析失败」，把「依赖未装」
误导成「配置写坏」；同时 ``--check`` 的依赖校验排在结构预检之后，用户先看到
的是错误方向的提示。

本文件锁定两条行为：
1. 缺 PyYAML 时 ``precheck()`` 跳过知识包语法预检并明确说明，**不**报解析失败；
2. ``--check`` 缺 PyYAML 时优先给出「pip install -r requirements.txt」指引。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gcs_install_order", ROOT / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


class TestMissingPyYAML(unittest.TestCase):
    """模拟「系统 Python 未安装 PyYAML」的全新机器场景。"""

    def _no_yaml(self):
        # sys.modules['yaml'] = None 会让 `import yaml` 抛 ImportError
        return mock.patch.dict(sys.modules, {"yaml": None})

    def test_precheck_skips_knowledge_check_without_pyyaml(self):
        """precheck 不应把「缺依赖」误报为「知识包解析失败」。"""
        buf = io.StringIO()
        with self._no_yaml(), contextlib.redirect_stdout(buf):
            install.precheck(ROOT)  # 不应抛 InstallError
        out = buf.getvalue()
        self.assertNotIn("知识包解析失败", out)
        self.assertIn("跳过", out)

    def test_check_reports_dependency_missing_first(self):
        """--check 缺 PyYAML 时应先报依赖缺失，并给出 pip 安装指引。"""
        buf = io.StringIO()
        with self._no_yaml(), contextlib.redirect_stdout(buf):
            rc = install.main(["--check"])
        out = buf.getvalue()
        self.assertEqual(rc, 1, "--check 缺依赖时应以退出码 1 结束")
        self.assertIn("PyYAML", out)
        self.assertIn("pip install -r requirements.txt", out)
        self.assertNotIn("知识包解析失败", out)

    def test_precheck_still_rejects_broken_yaml(self):
        """YAML 真坏时仍然必须报解析失败（本次修复不能放宽校验）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in install.REQUIRED:
                dst = root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                src = ROOT / rel
                dst.write_bytes(src.read_bytes() if src.is_file() else b"")
            kb = root / "knowledge" / "ningxia_cluster.yaml"
            kb.parent.mkdir(parents=True, exist_ok=True)
            kb.write_text("pools: [unclosed\n", encoding="utf-8")
            with self.assertRaises(install.InstallError) as ctx:
                install.precheck(root)
            self.assertIn("知识包解析失败", str(ctx.exception.message))


if __name__ == "__main__":
    unittest.main()
