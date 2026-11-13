"""操作台 HTTP 异常边界与鉴权集成测试（P2-3.5 + GCS-CHG-2026-002）。

用真实 ``ThreadingHTTPServer`` + ``http.client`` 发请求，覆盖：
分级鉴权 401/200、三态模式、413 请求体上限、400 参数/路径校验、
评测看板降级、历史运行磁盘回落、工具审计列。
"""

from __future__ import annotations

import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import server  # noqa: E402
from app.auth import AuthConfig, hash_token  # noqa: E402

TOKEN = "integration-test-token-0123456789abcdef"
HASH = hash_token(TOKEN)
SMALL_RUN = {"jobs": 10, "seed": 7, "policy": "generic"}


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._runs = server.RUNS_DIR
        cls._auth = server.Handler.auth_config
        cls._log = server.Handler.log_message
        server.RUNS_DIR = Path(cls.tmp.name)
        server.Handler.log_message = lambda self, fmt, *args: None
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        server.RUNS_DIR = cls._runs
        server.Handler.auth_config = cls._auth
        server.Handler.log_message = cls._log
        cls.tmp.cleanup()

    def setUp(self):
        self.set_mode("required")
        with server._LOCK:
            server._STORE.clear()

    def set_mode(self, mode, **extra):
        env = {"GCS_AUTH_MODE": mode}
        if mode != "off":
            env["GCS_AUTH_TOKEN_HASH"] = HASH
        env.update(extra)
        server.Handler.auth_config = AuthConfig(env=env, root=Path(self.tmp.name))

    def req(self, method, path, body=None, token=None, headers=None):
        h = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        if body is not None:
            h.setdefault("Content-Type", "application/json")
            h["Content-Length"] = str(len(body))
        if token:
            h["Authorization"] = "Bearer " + token
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=h)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    # ---------------------------------------------------------------- 鉴权
    def test_public_readonly_get_never_requires_token(self):
        for path in ("/", "/index.html", "/api/policies", "/api/eval", "/api/runs"):
            code, _h, _b = self.req("GET", path)
            self.assertEqual(code, 200, f"公开只读接口 {path} 不应要求凭据")

    def test_protected_write_without_token_401_with_challenge(self):
        code, h, b = self.req("POST", "/api/run", SMALL_RUN)
        self.assertEqual(code, 401)
        self.assertIn("Bearer", h.get("WWW-Authenticate", ""))
        self.assertIn("未授权", b.decode("utf-8"))
        # 统一文案，不回显具体失败原因（避免探测）
        self.assertNotIn("reason", b.decode("utf-8"))

    def test_wrong_token_401(self):
        code, _h, _b = self.req("POST", "/api/run", SMALL_RUN, token="wrong")
        self.assertEqual(code, 401)

    def test_protected_read_requires_token(self):
        self.assertEqual(self.req("GET", "/api/run/run_20261113_120000_full")[0], 401)
        self.assertEqual(self.req("GET", "/console/run_20261113_120000_full")[0], 401)

    def test_correct_token_200_and_tool_audit_column(self):
        code, _h, b = self.req("POST", "/api/run", SMALL_RUN, token=TOKEN)
        self.assertEqual(code, 200, b.decode("utf-8")[:300])
        d = json.loads(b)
        self.assertTrue(d["run_id"].startswith("run_"))
        self.assertGreater(len(d["events"]), 0)
        self.assertIn("tool", d["events"][0], "轨迹事件应带使用工具审计字段")
        self.assertIn("metrics", d)

    def test_loopback_mode_exempts_local_request(self):
        self.set_mode("loopback")
        code, _h, _b = self.req("POST", "/api/run", SMALL_RUN)
        self.assertEqual(code, 200)

    def test_off_mode_allows_all(self):
        self.set_mode("off")
        code, _h, _b = self.req("POST", "/api/run", SMALL_RUN)
        self.assertEqual(code, 200)

    # ---------------------------------------------------------------- 边界
    def test_body_over_limit_413(self):
        h = {"Content-Length": str(server.MAX_BODY + 1),
             "Authorization": "Bearer " + TOKEN,
             "Content-Type": "application/json"}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("POST", "/api/run", body=None, headers=h)
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 413)
        finally:
            conn.close()

    def test_invalid_json_400(self):
        code, _h, _b = self.req("POST", "/api/run", b"{bad-json", token=TOKEN)
        self.assertEqual(code, 400)

    def test_non_object_body_400(self):
        code, _h, _b = self.req("POST", "/api/run", b"[1,2]", token=TOKEN)
        self.assertEqual(code, 400)

    def test_unknown_policy_400(self):
        code, _h, _b = self.req("POST", "/api/run",
                                {"jobs": 10, "policy": "no_such_policy"},
                                token=TOKEN)
        self.assertEqual(code, 400)

    def test_illegal_run_id_400_path_traversal(self):
        code, _h, _b = self.req("GET", "/api/run/../../etc/passwd", token=TOKEN)
        self.assertEqual(code, 400)
        code2, _h2, _b2 = self.req("GET", "/console/..%2f..%2fetc", token=TOKEN)
        self.assertEqual(code2, 400)

    def test_eval_board_degrades_when_summary_missing(self):
        with patch.object(server, "eval_board",
                          lambda: {"available": False, "message": "尚未运行对照实验"}):
            code, _h, b = self.req("GET", "/api/eval")
        self.assertEqual(code, 200)
        self.assertIn("尚未运行", b.decode("utf-8"))

    def test_history_run_falls_back_to_disk_after_store_reset(self):
        code, _h, b = self.req("POST", "/api/run", SMALL_RUN, token=TOKEN)
        self.assertEqual(code, 200)
        rid = json.loads(b)["run_id"]
        with server._LOCK:
            server._STORE.clear()
        code2, _h2, b2 = self.req("GET", "/api/run/" + rid, token=TOKEN)
        self.assertEqual(code2, 200, "重启后应能从磁盘回读历史运行")
        self.assertEqual(json.loads(b2)["run_id"], rid)

    def test_runs_list_includes_new_run(self):
        code, _h, b = self.req("POST", "/api/run", SMALL_RUN, token=TOKEN)
        self.assertEqual(code, 200)
        rid = json.loads(b)["run_id"]
        code2, _h2, b2 = self.req("GET", "/api/runs")
        self.assertEqual(code2, 200)
        runs = [x["run_id"] for x in json.loads(b2)["runs"]]
        self.assertIn(rid, runs)


if __name__ == "__main__":
    unittest.main()
