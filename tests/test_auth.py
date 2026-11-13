"""静态令牌鉴权核心单元测试（GCS-CHG-2026-002）。

覆盖：令牌派生与常量时间校验、Bearer 解析、回环判定、分级路由、
三态模式与 fail-closed、过期分级处置、24 小时双凭据窗口、.env 优先级、
凭据生成/轮换工具。
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import auth, auth_tool  # noqa: E402
from app.auth import (  # noqa: E402
    AuthConfig, generate_token, hash_token, is_loopback, parse_bearer,
    requires_auth, rotation_deadline, verify_token,
)


class TestTokenPrimitives(unittest.TestCase):
    def test_generate_hash_verify(self):
        t = generate_token()
        h = hash_token(t)
        self.assertGreaterEqual(len(t), 40)          # 256 bit 熵
        self.assertNotIn(t, h)                       # .env 不含明文
        self.assertEqual(h.count("$"), 1)
        self.assertTrue(verify_token(t, h))
        self.assertFalse(verify_token(t + "x", h))
        self.assertFalse(verify_token("", h))

    def test_bad_stored_format_fails_closed(self):
        for bad in ("", "plain", "$deadbeef", "salt$"):
            self.assertFalse(verify_token("t", bad))

    def test_salt_differs_per_call(self):
        t = "same-token"
        self.assertNotEqual(hash_token(t), hash_token(t))
        self.assertTrue(verify_token(t, hash_token(t)))

    def test_parse_bearer(self):
        self.assertEqual(parse_bearer("Bearer abc.def"), "abc.def")
        self.assertEqual(parse_bearer("bearer abc.def"), "abc.def")
        self.assertEqual(parse_bearer("Basic abc"), "")
        self.assertEqual(parse_bearer(None), "")
        self.assertEqual(parse_bearer("Bearer"), "")

    def test_is_loopback(self):
        for addr in (("127.0.0.1", 1234), ("127.0.0.9", 1), ("::1", 1),
                     ("localhost", 1)):
            self.assertTrue(is_loopback(addr), addr)
        for addr in (("10.0.0.5", 1), ("192.168.1.2", 1), ("", 1)):
            self.assertFalse(is_loopback(addr), addr)

    def test_graded_routes(self):
        # 公开只读：CI 探测 / 深链接 / 作品证据展示不受影响
        for p in ("/", "/index.html", "/api/policies", "/api/eval", "/api/runs"):
            self.assertFalse(requires_auth("GET", p), p)
        # 受保护：唯一写接口 + 完整轨迹 + 看板文件
        self.assertTrue(requires_auth("POST", "/api/run"))
        self.assertTrue(requires_auth("GET", "/api/run/run_20261113_120000_full"))
        self.assertTrue(requires_auth("GET", "/console/run_20261113_120000_full"))
        self.assertFalse(requires_auth("POST", "/other"))
        self.assertFalse(requires_auth("GET", "/unknown"))


class TestAuthConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.token = generate_token()
        self.h = hash_token(self.token)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **env) -> AuthConfig:
        return AuthConfig(env=env, root=self.root)

    def test_off_mode_allows_everything_but_warns(self):
        cfg = self.cfg(GCS_AUTH_MODE="off")
        self.assertFalse(cfg.enabled)
        self.assertTrue(cfg.may_start)
        self.assertTrue(cfg.check({}, ("10.0.0.5", 1), "POST", "/api/run").ok)
        self.assertTrue(any("off" in m for m in cfg.startup_messages()))

    def test_default_mode_is_loopback(self):
        cfg = self.cfg()
        self.assertEqual(cfg.mode, "loopback")
        self.assertEqual(auth.DEFAULT_MODE, "loopback")
        self.assertEqual(auth.ROTATION_WINDOW_HOURS, 24)
        self.assertEqual(cfg.max_age_days, 90)

    def test_loopback_exempts_local_but_blocks_remote_without_token(self):
        cfg = self.cfg(GCS_AUTH_MODE="loopback", GCS_AUTH_TOKEN_HASH=self.h)
        self.assertTrue(cfg.check({}, ("127.0.0.1", 1), "POST", "/api/run").ok)
        self.assertFalse(cfg.check({}, ("10.0.0.5", 1), "POST", "/api/run").ok)
        self.assertTrue(cfg.check({"Authorization": "Bearer " + self.token},
                                  ("10.0.0.5", 1), "POST", "/api/run").ok)

    def test_required_rejects_all_protected_without_token(self):
        cfg = self.cfg(GCS_AUTH_MODE="required", GCS_AUTH_TOKEN_HASH=self.h)
        self.assertTrue(cfg.check({}, ("127.0.0.1", 1), "GET", "/").ok)  # 公开
        self.assertFalse(cfg.check({}, ("127.0.0.1", 1), "POST", "/api/run").ok)
        self.assertTrue(cfg.check({"Authorization": "Bearer " + self.token},
                                  ("127.0.0.1", 1), "POST", "/api/run").ok)

    def test_required_without_credentials_is_fail_closed(self):
        cfg = self.cfg(GCS_AUTH_MODE="required")
        self.assertFalse(cfg.may_start)
        self.assertTrue(any("ERROR" in m for m in cfg.startup_messages()))
        # 绝不能因为「没配 key」而像 LLM 网关那样降级放行
        r = cfg.check({}, ("127.0.0.1", 1), "POST", "/api/run")
        self.assertFalse(r.ok)

    def test_loopback_without_credentials_warns_not_blocks_startup(self):
        cfg = self.cfg(GCS_AUTH_MODE="loopback")
        self.assertTrue(cfg.may_start)
        self.assertTrue(any("WARN" in m for m in cfg.startup_messages()))
        self.assertFalse(cfg.check({}, ("10.0.0.5", 1), "POST", "/api/run").ok)

    def test_unknown_mode_fails_closed_to_required(self):
        cfg = self.cfg(GCS_AUTH_MODE="typo")
        self.assertEqual(cfg.mode, "required")
        self.assertFalse(cfg.may_start)

    def test_expiry_loopback_warns_required_blocks(self):
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        loop = self.cfg(GCS_AUTH_MODE="loopback", GCS_AUTH_TOKEN_HASH=self.h,
                        GCS_AUTH_ISSUED_AT=old)
        self.assertTrue(loop.expired)
        self.assertTrue(loop.may_start)          # 仅告警
        req = self.cfg(GCS_AUTH_MODE="required", GCS_AUTH_TOKEN_HASH=self.h,
                       GCS_AUTH_ISSUED_AT=old)
        self.assertFalse(req.may_start)          # 拒绝启动

    def test_fresh_credential_not_expired(self):
        now = datetime.now(timezone.utc).isoformat()
        cfg = self.cfg(GCS_AUTH_MODE="required", GCS_AUTH_TOKEN_HASH=self.h,
                       GCS_AUTH_ISSUED_AT=now)
        self.assertFalse(cfg.expired)
        self.assertTrue(cfg.may_start)

    def test_rotation_window_accepts_prev_then_ignores_expired_prev(self):
        old_token = generate_token()
        old_h = hash_token(old_token)
        until = rotation_deadline(24)
        cfg = self.cfg(GCS_AUTH_MODE="required", GCS_AUTH_TOKEN_HASH=self.h,
                       GCS_AUTH_TOKEN_PREV_HASH=old_h,
                       GCS_AUTH_PREV_VALID_UNTIL=until)
        self.assertTrue(cfg.prev_valid)
        self.assertTrue(cfg.check({"Authorization": "Bearer " + old_token},
                                  ("127.0.0.1", 1), "POST", "/api/run").ok)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        cfg2 = self.cfg(GCS_AUTH_MODE="required", GCS_AUTH_TOKEN_HASH=self.h,
                        GCS_AUTH_TOKEN_PREV_HASH=old_h,
                        GCS_AUTH_PREV_VALID_UNTIL=past)
        self.assertFalse(cfg2.prev_valid)
        self.assertFalse(cfg2.check({"Authorization": "Bearer " + old_token},
                                    ("127.0.0.1", 1), "POST", "/api/run").ok)

    def test_no_prev_window_by_default(self):
        cfg = self.cfg(GCS_AUTH_MODE="required", GCS_AUTH_TOKEN_HASH=self.h,
                       GCS_AUTH_TOKEN_PREV_HASH=hash_token("other"))
        self.assertFalse(cfg.prev_valid)   # 缺 VALID_UNTIL 不启用窗口

    def test_system_env_wins_over_dotenv(self):
        (self.root / ".env").write_text(
            "GCS_AUTH_MODE=off\nGCS_AUTH_TOKEN_HASH=from-file\n", encoding="utf-8")
        with patch.dict(os.environ, {"GCS_AUTH_MODE": "required"}):
            cfg = AuthConfig(env={}, root=self.root)
        self.assertEqual(cfg.mode, "required")
        self.assertEqual(cfg.token_hash, "from-file")


class TestAuthTool(unittest.TestCase):
    def test_generate_rotate_and_no_plaintext_in_env(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(auth_tool, "harden", lambda p: None):
            p = Path(d) / ".env"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(auth_tool.cmd_generate(p), 0)
            env1 = auth_tool.read_env(p)
            self.assertIn("GCS_AUTH_TOKEN_HASH", env1)
            self.assertEqual(env1["GCS_AUTH_MODE"], "loopback")
            self.assertTrue(env1["GCS_AUTH_ISSUED_AT"])

            # 不允许静默覆盖已有凭据
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(auth_tool.cmd_generate(p), 2)

            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(auth_tool.cmd_rotate(p, hours=24), 0)
                printed = buf.getvalue()
            env2 = auth_tool.read_env(p)
            self.assertNotEqual(env1["GCS_AUTH_TOKEN_HASH"],
                                env2["GCS_AUTH_TOKEN_HASH"])
            self.assertEqual(env1["GCS_AUTH_TOKEN_HASH"],
                             env2["GCS_AUTH_TOKEN_PREV_HASH"])
            self.assertIn("GCS_AUTH_PREV_VALID_UNTIL", env2)
            self.assertIn("访问凭据", printed)

            # .env 里只有派生值，不含明文口令；status 也不打印派生值
            body = p.read_text(encoding="utf-8")
            self.assertNotIn("sk-", body)
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(auth_tool.cmd_status(p), 0)
                status = buf.getvalue()
            self.assertNotIn(env2["GCS_AUTH_TOKEN_HASH"], status)
            self.assertIn("鉴权模式", status)

    def test_upsert_preserves_other_keys_and_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# comment\nGCS_LLM_MODEL=deepseek-chat\n"
                         "GCS_AUTH_MODE=off\n", encoding="utf-8")
            auth_tool.upsert_env(p, {"GCS_AUTH_MODE": "required",
                                     "GCS_AUTH_TOKEN_HASH": "a$b"})
            text = p.read_text(encoding="utf-8")
            self.assertIn("# comment", text)
            self.assertIn("GCS_LLM_MODEL=deepseek-chat", text)
            self.assertIn("GCS_AUTH_MODE=required", text)
            self.assertIn("GCS_AUTH_TOKEN_HASH=a$b", text)
            self.assertEqual(text.count("GCS_AUTH_MODE="), 1)


if __name__ == "__main__":
    unittest.main()
