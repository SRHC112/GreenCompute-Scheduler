# -*- coding: utf-8 -*-
"""install.py 关键纯函数测试：.env 合并写入（M8 口径统一）。"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gcs_install", ROOT / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)

LLM = {"GCS_LLM_BASE_URL": "https://api.deepseek.com/v1",
       "GCS_LLM_API_KEY": "sk-test", "GCS_LLM_MODEL": "deepseek-chat"}


class TestWriteLlmEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_preserves_auth_keys_and_adds_llm(self):
        self.env.write_text("GCS_AUTH_MODE=required\nGCS_AUTH_TOKEN_HASH=abc$def\n", encoding="utf-8")
        install.write_llm_env(self.env, LLM)
        t = self.env.read_text(encoding="utf-8")
        self.assertIn("GCS_AUTH_MODE=required", t)
        self.assertIn("GCS_AUTH_TOKEN_HASH=abc$def", t)
        for k, v in LLM.items():
            self.assertIn(f"{k}={v}", t)

    def test_creates_file_when_absent(self):
        install.write_llm_env(self.env, LLM)
        t = self.env.read_text(encoding="utf-8")
        self.assertIn("GCS_LLM_API_KEY=sk-test", t)
        # P3-9：探针写失败时用 os.access 复核，区分「真不可写」与「被拦截/误报」
        from unittest import mock
        with mock.patch.object(install, "REQUIRED", ()), \
                mock.patch.object(Path, "write_text", side_effect=OSError("denied")), \
                mock.patch.object(install.os, "access", return_value=False):
            with self.assertRaises(install.InstallError) as ctx:
                install.precheck(Path(self.tmp.name))
            self.assertIn("不可写", str(ctx.exception))
        with mock.patch.object(install, "REQUIRED", ()), \
                mock.patch.object(Path, "write_text", side_effect=OSError("blocked")), \
                mock.patch.object(install.os, "access", return_value=True):
            with self.assertRaises(install.InstallError) as ctx:
                install.precheck(Path(self.tmp.name))
            self.assertIn("写入探针失败", str(ctx.exception))
        install.setup_console()  # P3-15：chcp 改 argv 列表后仍不抛异常
        # 一致性更新：--check 的 PyYAML 校验范围 / 失败条件 / 可操作提示
        from unittest import mock
        import yaml
        self.assertTrue(install.check_runtime_deps().startswith("6."))
        with mock.patch("importlib.util.find_spec", return_value=None):
            with self.assertRaises(install.InstallError) as ctx:
                install.check_runtime_deps()
        self.assertIn("PyYAML", ctx.exception.message)
        self.assertIn("pip install -r requirements.txt", ctx.exception.hint)
        with mock.patch.object(yaml, "__version__", "5.4.1"):
            with self.assertRaises(install.InstallError) as ctx:
                install.check_runtime_deps()
        self.assertIn("版本不兼容", ctx.exception.message)
        # Windows .env 权限：授权失败时不执行破坏性的 /inheritance:r
        # 平台守卫：icacls 分支只在 Windows 存在；Linux/macOS 走 os.chmod，
        # 不会调用 subprocess，故以下断言必须限定 os.name == "nt"
        # （2026-11-15 GitHub runner 实测：无守卫时 Linux 上 4 个组合均报
        #  status 期望 "failed"、实际 "ok"，本地 Windows 全绿而 CI 全红）
        if os.name == "nt":
            calls = []
            with mock.patch.object(install.subprocess, "run",
                                   side_effect=lambda argv, **kw: (
                                       calls.append(list(argv)) or mock.Mock(returncode=1))):
                status = install.harden_env_permissions(self.env, notify=lambda m: None)
            self.assertEqual(status, "failed")
            self.assertEqual(len(calls), 1)
            self.assertIn("/grant:r", calls[0])
            self.assertNotIn("/inheritance:r", calls[0])
        else:
            # Unix 分支的真实覆盖：chmod 600 且返回 ok
            status = install.harden_env_permissions(self.env, notify=lambda m: None)
            self.assertEqual(status, "ok")
            self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertIn("GCS_LLM_API_KEY=sk-test", self.env.read_text(encoding="utf-8"))
        # 移除继承失败后 .env 仍可读；且已有 .env 不可读时不静默清空
        if os.name == "nt":
            calls2 = []
            with mock.patch.object(install.subprocess, "run",
                                   side_effect=lambda argv, **kw: (
                                       calls2.append(list(argv)) or
                                       mock.Mock(returncode=0 if "/grant:r" in argv else 1))):
                status = install.harden_env_permissions(self.env, notify=lambda m: None)
            self.assertEqual(status, "failed")
            self.assertEqual(len(calls2), 2)
            self.assertIn("/inheritance:r", calls2[1])
        self.assertTrue(self.env.read_text(encoding="utf-8"))
        with mock.patch("pathlib.Path.read_text", side_effect=OSError("denied")):
            with self.assertRaises(install.InstallError) as ctx:
                install.write_llm_env(self.env, LLM)
        self.assertIn("鉴权配置", ctx.exception.hint)
        # P3 闭环：非 UTF-8 .env 的读取 / 回读 / 降级行为
        import contextlib
        import io
        bad = self.env.parent / "bad_env"
        raw = b"GCS_AUTH_MODE=required\n\xb6\xc7\xd7\xd6\n"
        bad.write_bytes(raw)
        with self.assertRaises(install.InstallError) as ctx:
            install.write_llm_env(bad, LLM)
        self.assertIn("UTF-8", ctx.exception.message)
        self.assertIn("鉴权配置", ctx.exception.hint)
        self.assertEqual(bad.read_bytes(), raw, "非法编码 .env 不得被改写")
        self.assertFalse(install._env_readable(bad), "解码异常应返回 False 而非抛出")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            env2 = {}
            install.load_dotenv(bad, env2)
        self.assertEqual(env2, {})
        self.assertIn("UnicodeDecodeError", err.getvalue())
        # 以下三段均为 Windows icacls 分支专有：Linux/macOS 走 os.chmod，
        # 不调用 subprocess，故必须限定 os.name == "nt"
        if os.name == "nt":
            # P3 闭环：回滚失败告警不得误报「已回滚」
            warns = []

            def run3(argv, **kw):
                if "/inheritance:e" in argv:
                    raise OSError("restore failed")
                return mock.Mock(returncode=0)

            with mock.patch.object(install.subprocess, "run", side_effect=run3), \
                 mock.patch.object(install, "_env_readable", side_effect=[True, False]):
                st = install.harden_env_permissions(self.env, notify=warns.append)
            self.assertEqual(st, "failed")
            self.assertTrue(any("回滚失败" in w for w in warns))
            self.assertFalse(any("已回滚 ACL" in w for w in warns))
            # P3 闭环：rc 非 0 分支也做回读自检（可读时不额外回滚）
            calls3 = []
            with mock.patch.object(install.subprocess, "run",
                                   side_effect=lambda a, **kw: (
                                       calls3.append(list(a)) or mock.Mock(returncode=1))), \
                 mock.patch.object(install, "_env_readable",
                                   wraps=install._env_readable) as rd:
                st = install.harden_env_permissions(self.env, notify=lambda m: None)
            self.assertEqual(st, "failed")
            self.assertGreaterEqual(rd.call_count, 1)
            self.assertEqual(len(calls3), 1)
            # 成功路径：必须「先 /grant:r 授权、再 /inheritance:r 移除继承」，返回 ok
            calls_ok = []
            with mock.patch.object(install.subprocess, "run",
                                   side_effect=lambda argv, **kw: (
                                       calls_ok.append(list(argv)) or mock.Mock(returncode=0))):
                st_ok = install.harden_env_permissions(self.env, notify=lambda m: None)
            self.assertEqual(st_ok, "ok")
            self.assertEqual(len(calls_ok), 2)
            self.assertIn("/grant:r", calls_ok[0])
            self.assertIn("/inheritance:r", calls_ok[1])
        # POSIX 分支：chmod 600 + 回读自检
        chmod_args = []
        with mock.patch.object(install.os, "name", "posix"), \
             mock.patch.object(install.os, "chmod",
                               side_effect=lambda p, m: chmod_args.append((p, m))):
            st_posix = install.harden_env_permissions(self.env, notify=lambda m: None)
        self.assertEqual(st_posix, "ok")
        self.assertEqual(chmod_args[0][1], 0o600)
        # 离线路径不得触碰 .env ACL：不接入大模型 → 不创建 .env、icacls 调用 0 次
        fresh = self.env.parent / "offline_case"
        fresh.mkdir(exist_ok=True)
        offline_calls = []
        with mock.patch.object(install.subprocess, "run",
                               side_effect=lambda argv, **kw: (
                                   offline_calls.append(list(argv)) or mock.Mock(returncode=0))), \
             mock.patch.object(install, "probe_llm_mode",
                               return_value="offline-mock|deepseek-chat"):
            install.configure_llm(fresh, sys.executable, mock.Mock(llm=False, no_llm=True))
        self.assertEqual([c for c in offline_calls if c and "icacls" in str(c[0]).lower()], [])
        self.assertFalse((fresh / ".env").exists(), "离线路径不应创建 .env")

    def test_overwrites_existing_llm_keys_without_duplication(self):
        self.env.write_text("GCS_AUTH_MODE=loopback\nGCS_LLM_API_KEY=old\n", encoding="utf-8")
        install.write_llm_env(self.env, LLM)
        t = self.env.read_text(encoding="utf-8")
        self.assertNotIn("GCS_LLM_API_KEY=old", t)
        self.assertEqual(t.count("GCS_LLM_API_KEY="), 1)


if __name__ == "__main__":
    unittest.main()
