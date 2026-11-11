"""模型网关：统一走国产模型的 OpenAI 兼容接口，离线时退化为确定性模板。

约定（很重要，决定了“可复现”能不能成立）：
- **所有数字由确定性模块计算**，模型只负责“归因叙述”和语义层 Judge；
- 模型不可用时整个流程仍能跑完，评测得分数值不变，只有叙述文本变化；
- 每次调用都记账（token、耗时），超过预算由编排器熔断。

环境变量：
    GCS_LLM_BASE_URL   例如 https://api.deepseek.com/v1
    GCS_LLM_API_KEY    你的 key
    GCS_LLM_MODEL      例如 deepseek-chat / qwen-plus / doubao-pro-32k
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


class BudgetExceeded(RuntimeError):
    pass


class ModelGateway:
    def __init__(self, token_budget: int = 600_000, model: str | None = None):
        self.base_url = os.getenv("GCS_LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("GCS_LLM_API_KEY", "")
        self.model = model or os.getenv("GCS_LLM_MODEL", "deepseek-chat")
        self.token_budget = token_budget
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0
        self.latency_ms = 0.0
        self.online = bool(self.base_url and self.api_key)
        self.mode = "online" if self.online else "offline-mock"

    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, temperature: float = 0.2) -> str:
        self._check_budget()
        if self.online:
            try:
                text = self._request(system, user, temperature)
            except Exception as exc:  # 网络/额度异常一律降级，不让演示挂掉
                self.mode = f"degraded({type(exc).__name__})"
                text = self._mock(system, user)
        else:
            text = self._mock(system, user)
        self.calls += 1
        self.tokens_in += _count(system + user)
        self.tokens_out += _count(text)
        return text

    def _request(self, system: str, user: str, temperature: float) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
        }).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(3):  # 限流退避：0.5s / 1.5s / 3s
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
            )
            try:
                started = time.time()
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.latency_ms += (time.time() - started) * 1000
                return data["choices"][0]["message"]["content"].strip()
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as exc:
                last_error = exc
                time.sleep(0.5 * (3 ** attempt))
        raise last_error or RuntimeError("模型调用失败")

    def _mock(self, system: str, user: str) -> str:
        """离线确定性模板。

        注意两点：
        1. 叙述类请求（报告撰写）直接把已算好的事实复述成一句话，不编造任何数字；
        2. 判定类请求（语义 Judge）必须返回 PASS / FAIL 供上层解析，
           否则 L5 判定会失效——这是离线退化时最容易踩的坑。
        """
        first = user.strip().splitlines()[0] if user.strip() else ""
        if "PASS" in system or "FAIL" in system:
            # 语义 Judge：离线时依据“结论与依据是否自洽”做规则判定
            ok = ("选定" in first) and ("排除" in first)
            return "PASS" if ok else "FAIL"
        if first.startswith("本批次"):
            return first
        return f"[离线模式] 已按知识包与硬约束完成「{first[:40]}」，结论由确定性模块给出，本段为模板叙述。"

    def _check_budget(self) -> None:
        if self.tokens_in + self.tokens_out >= self.token_budget:
            raise BudgetExceeded(
                f"token 预算耗尽：{self.tokens_in + self.tokens_out}/{self.token_budget}")

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "mode": self.mode,
            "model": self.model,
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_in + self.tokens_out,
            "latency_ms": round(self.latency_ms, 1),
            "budget": self.token_budget,
        }


def _count(text: str) -> int:
    """粗估 token：中文约 1 字 1 token，英文约 4 字符 1 token，取保守值。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4 + 1
