# -*- coding: utf-8 -*-
"""模型调用「重试策略 / 降级可见性 / 启动预检」测试（2026-11-16）。

背景（实机问题）：
用户接入 DeepSeek 后 `run.bat demo` 长时间无输出。定位结果是
① `.env` 中密钥损坏（1 字节控制符）与模型名非官方 → 服务端返回 4xx；
② `OpenAICompatAdapter.complete` 把 **4xx 也当限流重试**（3 次 + 退避 5s），
   网络挂起时单次调用更达 60s×3 ≈ 185s；
③ 降级只写进产物字段，终端无任何提示，用户误以为"跑完了"。

修复后的行为（本文件锁定）：
1. 4xx（429 除外）**不重试**，立即上抛；
2. 429 / 5xx / 网络超时仍按 0.5s/1.5s/3s 退避重试；
3. 单次请求默认超时 15s（原 60s）；
4. 首次降级向 stderr 打印一条中文 `[WARN]`，连续调用不重复打印；
5. `probe()` 预检：401/403 判失败（附鉴权提示）、404/405 判通过（不阻断）、2xx 通过。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gcs_llm_retry", ROOT / "gcs" / "llm.py")
llm = importlib.util.module_from_spec(_spec)
sys.modules["gcs_llm_retry"] = llm
_spec.loader.exec_module(llm)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x/v1/chat/completions", code, "err", {}, None)


class TestRetryPolicy(unittest.TestCase):
    def _calls(self, error: Exception) -> int:
        calls: list[int] = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise error

        adapter = llm.OpenAICompatAdapter(timeout=5, max_retries=3)
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(llm.time, "sleep", return_value=None):
            with self.assertRaises(Exception):
                adapter.complete(base_url="https://x/v1", api_key="k", model="m",
                                 system="s", user="u", temperature=0)
        return len(calls)

    def test_4xx_is_not_retried(self):
        """401/403/400/404 属永久错误，必须只发一次请求。"""
        for code in (400, 401, 403, 404):
            self.assertEqual(self._calls(_http_error(code)), 1, f"HTTP {code} 不应重试")

    def test_429_and_5xx_are_retried(self):
        for code in (429, 500, 502):
            self.assertEqual(self._calls(_http_error(code)), 3, f"HTTP {code} 应重试 3 次")

    def test_network_timeout_is_retried(self):
        self.assertEqual(self._calls(TimeoutError("timed out")), 3)

    def test_default_timeout_is_15s(self):
        self.assertEqual(llm.OpenAICompatAdapter().timeout, 15)


class TestDegradeWarning(unittest.TestCase):
    def _gateway(self) -> "llm.ModelGateway":
        gw = llm.ModelGateway(token_budget=10_000_000)
        gw.base_url, gw.api_key = "https://x/v1", "k"
        gw.online, gw.base_url_secure, gw.mode = True, True, "online"
        return gw

    def test_warns_once_even_after_many_failures(self):
        gw = self._gateway()
        buf = io.StringIO()
        with mock.patch.object(gw, "_request", side_effect=_http_error(401)), \
             contextlib.redirect_stderr(buf):
            for _ in range(3):
                gw.chat("system", "user")
        out = buf.getvalue()
        self.assertEqual(out.count("[WARN]"), 1, "降级提示应只打印一次")
        self.assertIn("已降级", out)
        self.assertIn("鉴权失败", out)
        self.assertEqual(gw.mode, "degraded(HTTPError)")

    def test_404_hint_mentions_model(self):
        gw = self._gateway()
        buf = io.StringIO()
        with mock.patch.object(gw, "_request", side_effect=_http_error(404)), \
             contextlib.redirect_stderr(buf):
            gw.chat("system", "user")
        self.assertIn("模型或路径不存在", buf.getvalue())


class TestProbe(unittest.TestCase):
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _gateway(self) -> "llm.ModelGateway":
        gw = llm.ModelGateway(token_budget=10_000_000)
        gw.base_url, gw.api_key = "https://x/v1", "k"
        gw.online, gw.base_url_secure = True, True
        return gw

    def test_2xx_passes(self):
        gw = self._gateway()
        with mock.patch.object(llm.urllib.request, "urlopen", return_value=self._Resp()):
            ok, msg = gw.probe()
        self.assertTrue(ok)

    def test_401_fails_with_actionable_message(self):
        gw = self._gateway()
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=_http_error(401)):
            ok, msg = gw.probe()
        self.assertFalse(ok)
        self.assertIn("鉴权失败", msg)
        self.assertIn("GCS_LLM_BASE_URL", msg)

    def test_404_treated_as_skip_not_failure(self):
        gw = self._gateway()
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=_http_error(404)):
            ok, msg = gw.probe()
        self.assertTrue(ok, "不提供 /models 的服务不应被判定为鉴权失败")
        self.assertIn("未提供", msg)

    def test_network_error_fails(self):
        gw = self._gateway()
        with mock.patch.object(llm.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("unreachable")):
            ok, msg = gw.probe()
        self.assertFalse(ok)
        self.assertIn("网络不可达", msg)

    def test_offline_gateway_probe_is_false(self):
        gw = llm.ModelGateway(token_budget=1000)
        gw.online = False
        ok, msg = gw.probe()
        self.assertFalse(ok)
        self.assertIn("未配置在线模型", msg)


if __name__ == "__main__":
    unittest.main()
