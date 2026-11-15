"""鉴权与 HTTP 服务端缺陷回归测试（2026 未来杯 GreenCompute-Scheduler）。

覆盖 G1/G2/G3；与 tests/ 既有文件不重名，可独立回滚。
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import AuthConfig, load_env_file  # noqa: E402
import app.server as server  # noqa: E402


class LoadEnvNonUtf8Test(unittest.TestCase):
    """G1：非 UTF-8 编码的 .env 不得让服务端启动抛裸 traceback。"""

    def _write_env(self, dirpath: Path, data: bytes) -> Path:
        p = dirpath / ".env"
        p.write_bytes(data)
        return p

    def test_non_utf8_env_loads_without_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_env(Path(tmp), b"GCS_AUTH_MODE=required\n\xb6\xc7\xd7\xd6\n")
            warnings: list[str] = []
            result = load_env_file(Path(tmp) / ".env", env={},
                                   use_os_environ=False, warn=warnings)
            # .env 读取失败降级为空配置，不抛异常
            self.assertEqual(result, {})
            self.assertTrue(
                any("不是 UTF-8 编码" in w and "请用 UTF-8 保存后重试" in w
                    for w in warnings), warnings)

    def test_auth_config_non_utf8_env_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_env(Path(tmp), b"GCS_AUTH_MODE=loopback\n\xb6\xc7\xd7\xd6\n")
            cfg = AuthConfig(env={}, root=Path(tmp))
            self.assertEqual(cfg.mode, "loopback")
            self.assertTrue(any("不是 UTF-8 编码" in w for w in cfg.warnings),
                            cfg.warnings)


class RenderPageSinglePassTest(unittest.TestCase):
    """G3：模板替换必须单遍，插入的值不再被后续占位符扫描。"""

    def test_placeholder_text_in_value_is_not_reinjected(self):
        orig_dag = server.DAG_JS
        try:
            server.DAG_JS = "<!-- 候选：$jobs ${x} {{y}} -->"
            out = server.render_page().decode("utf-8")
            self.assertIn("<!-- 候选：$jobs ${x} {{y}} -->", out)
            self.assertEqual(out.count("$jobs"), 1)
            self.assertEqual(out.count("${x}"), 1)
            self.assertEqual(out.count("{{y}}"), 1)
        finally:
            server.DAG_JS = orig_dag

    def test_standard_placeholders_still_rendered(self):
        out = server.render_page().decode("utf-8")
        self.assertNotIn("{ver}", out)
        self.assertNotIn("{dag_js}", out)
        self.assertNotIn("{metric_terms}", out)
        self.assertIn("MN.sla_rate", out)  # G5：KPI 副标题来自 metrics_meta NOTE


class JobsBoundaryTest(unittest.TestCase):
    """G2：jobs=1/300 通过，jobs=0/301 返回中文错误（400）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.RUNS_DIR = Path(cls.tmp.name)
        server.Handler.log_message = lambda self, fmt, *args: None
        server.Handler.auth_config = AuthConfig(env={"GCS_AUTH_MODE": "off"})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)

    def _post(self, payload):
        body = json.dumps(payload).encode("utf-8")
        h = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("POST", "/api/run", body=body, headers=h)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_jobs_1_and_300_200(self):
        for jobs_n in (1, 300):
            with patch.object(server, "run_once",
                              return_value={"run_id": "run_x", "events": [], "metrics": {}}):
                code, body = self._post({"jobs": jobs_n, "seed": 1, "policy": "generic"})
            self.assertEqual(code, 200, body[:300])

    def test_jobs_0_and_301_400_zh(self):
        for jobs_n in (0, 301):
            code, body = self._post({"jobs": jobs_n, "seed": 1, "policy": "generic"})
            self.assertEqual(code, 400, body[:300])
            text = body.decode("utf-8")
            self.assertIn("jobs", text)
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in text), text)


if __name__ == "__main__":
    unittest.main()
