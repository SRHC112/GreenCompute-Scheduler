# -*- coding: utf-8 -*-
"""依赖安装「网络自愈」与离线降级测试（2026-11-16 方案 5）。

背景：``install.py`` 完整安装需联网 ``pip install -r requirements.txt``（唯一依赖
PyYAML）。若目标机环境变量里存在**指向死代理**的 ``HTTP_PROXY``，pip 会以
``ProxyError`` / ``WinError 10061`` 失败，此前脚本只有一条兜底提示、不会自愈。

本文件锁定修复后的行为：
1. 代理失败能被准确识别，并走「清除代理 → 换镜像 → 离线降级」分级重试；
2. 环境变量只在副本中清除，不污染 ``os.environ``；
3. 全部联网方式失败时启用 ``vendor/yaml`` 纯 Python 副本，且**显式告警**；
4. 无离线副本时给出可操作的中文错误（含 pip 镜像命令）；
5. ``vendor/yaml`` 不含 C 扩展、可独立 import 并正确解析 YAML，且附带 MIT 许可证。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gcs_install_offline", ROOT / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


def _proc(returncode: int, out: str = "", err: str = "") -> mock.Mock:
    return mock.Mock(returncode=returncode, stdout=out, stderr=err)


class TestProxyHelpers(unittest.TestCase):
    def test_detects_proxy_failure(self):
        for text in (
            "ProxyError: Cannot connect to proxy",
            "[WinError 10061] 由于目标计算机积极拒绝，无法连接。",
            "NewConnectionError('<urllib3.connection.HTTPSConnection object>')",
            "Failed to establish a new connection",
        ):
            self.assertTrue(install._looks_like_proxy_failure(text), text)

    def test_ignores_non_proxy_failure(self):
        for text in ("No matching distribution found for PyYAML", "ERROR: Could not find a version", ""):
            self.assertFalse(install._looks_like_proxy_failure(text), text)

    def test_env_without_proxy_strips_all_cases(self):
        injected = {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
            "ALL_PROXY": "socks5://127.0.0.1:1",
            "PIP_INDEX_URL": "http://example.invalid/simple",
        }
        with mock.patch.dict(os.environ, injected, clear=False):
            env = install._env_without_proxy()
            # 原环境不得被修改
            self.assertEqual(os.environ["HTTP_PROXY"], "http://127.0.0.1:1")
        for key in ("HTTP_PROXY", "https_proxy", "ALL_PROXY"):
            self.assertNotIn(key, env)
        self.assertEqual(env.get("PIP_PROXY"), "")
        self.assertNotIn("PIP_INDEX_URL", env)


class TestInstallDepsRetry(unittest.TestCase):
    def test_clears_proxy_then_uses_mirror(self):
        """前两次失败（代理错误）→ 第三次走镜像源成功。"""
        calls: list[tuple[list[str], dict | None]] = []

        def fake_run(cmd, **kwargs):
            calls.append(([str(c) for c in cmd], kwargs.get("env")))
            if len(calls) < 3:
                return _proc(1, err="ProxyError: Cannot connect to proxy")
            return _proc(0, out="Successfully installed PyYAML-6.0.3")

        with mock.patch.object(install, "run", side_effect=fake_run), \
             mock.patch.object(install, "run_checked", return_value=_proc(0, "6.0.3")), \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            ver = install.install_deps(Path("."), Path(sys.executable))

        self.assertEqual(ver, "6.0.3")
        self.assertEqual(len(calls), 3, "应依次尝试三种联网方式")
        # 第 1 次沿用原环境（env 为 None）
        self.assertIsNone(calls[0][1])
        # 第 2、3 次必须清除代理
        self.assertEqual(calls[1][1].get("PIP_PROXY"), "")
        self.assertNotIn("HTTP_PROXY", calls[1][1])
        # 第 3 次必须带镜像源
        self.assertIn("-i", calls[2][0])
        self.assertIn(install.PIP_MIRROR, calls[2][0])
        self.assertIn("镜像源", buf.getvalue())

    def test_falls_back_to_vendored_yaml(self):
        """三种联网方式全失败 → 启用 vendor/yaml 离线降级并显式告警。"""
        with mock.patch.object(install, "run", return_value=_proc(1, "No matching distribution found")), \
             mock.patch.object(install, "_install_yaml_offline", return_value="6.0.3") as offline, \
             mock.patch.object(install, "run_checked", return_value=_proc(0, "6.0.3")), \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            ver = install.install_deps(Path("."), Path(sys.executable))

        self.assertEqual(ver, "6.0.3")
        offline.assert_called_once()
        out = buf.getvalue()
        self.assertIn("离线降级", out)
        self.assertIn("WARN", out)

    def test_raises_actionable_error_when_offline_unavailable(self):
        """无离线副本时必须抛可操作错误，且 hint 含 pip 镜像命令。"""
        with mock.patch.object(install, "run", return_value=_proc(1, "No matching distribution found")), \
             mock.patch.object(install, "_install_yaml_offline", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(install.InstallError) as ctx:
                install.install_deps(Path("."), Path(sys.executable))

        self.assertIn("依赖安装失败", ctx.exception.message)
        self.assertIn("pip install -r requirements.txt", ctx.exception.hint)
        self.assertIn(install.PIP_MIRROR, ctx.exception.hint)


class TestVendoredYaml(unittest.TestCase):
    """离线副本自身的正确性（不 mock，真实 import）。"""

    def test_vendor_dir_has_no_binary_extension(self):
        files = [p for p in Path(install.VENDOR_YAML_DIR).rglob("*") if p.is_file()]
        self.assertTrue(files, "vendor/yaml 不应为空")
        self.assertFalse(
            [p.name for p in files if p.suffix.lower() in (".pyd", ".so", ".dll", ".dylib")],
            "离线副本必须是纯 Python（不得携带平台相关二进制）",
        )
        self.assertTrue((Path(install.SCRIPT_DIR) / "vendor" / "LICENSE-PyYAML.txt").is_file(),
                        "必须随附 MIT 许可证原文")

    def test_vendor_yaml_imports_and_parses(self):
        """把 vendor/yaml 放进隔离目录后，必须能独立 import 并正确解析 YAML。"""
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(install.VENDOR_YAML_DIR, Path(tmp) / "yaml")
            env = {**os.environ, "PYTHONPATH": tmp, "PYTHONNOUSERSITE": "1"}
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import yaml; print(yaml.__file__); print(yaml.safe_load('a: [1, 2]'))"],
                capture_output=True, text=True, env=env, timeout=120,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("yaml", proc.stdout)
        self.assertIn("'a': [1, 2]", proc.stdout)


if __name__ == "__main__":
    unittest.main()
