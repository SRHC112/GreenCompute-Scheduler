"""模型网关：统一走国产模型的 OpenAI 兼容接口，离线时退化为确定性模板。

约定（很重要，决定了“可复现”能不能成立）：
- **所有数字由确定性模块计算**，模型只负责“归因叙述”和语义层 Judge；
- 模型不可用时整个流程仍能跑完，评测得分数值不变，只有叙述文本变化；
- 每次调用都记账（token、耗时），超过预算由网关自身降级回离线模板，流程不中断。

模型路由（SRS 8.1 AI-M01/M04、FR-M02-009）：
- 主模型（tier="primary"，默认）：归因叙述、语义 Judge 等复杂任务；
- 轻量模型（tier="light"）：短摘要等简单任务，未配置时自动回落主模型；
- 自定义适配器：继承 `OpenAICompatAdapter` 覆盖 `complete()`，经
  `ModelGateway(adapter=...)` 注入即可接管全部在线请求（不同 API 方言、
  自建代理、mock 测试均可），网关的记账/降级/预算逻辑不受影响。

环境变量：
    GCS_LLM_BASE_URL    例如 https://api.deepseek.com/v1
    GCS_LLM_API_KEY     你的 key
    GCS_LLM_MODEL       主模型，例如 deepseek-chat / qwen-plus / doubao-pro-32k
    GCS_LLM_LITE_MODEL  轻量模型，例如 qwen-turbo / glm-4-flash；留空回落主模型
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


class BudgetExceeded(RuntimeError):
    """模型调用预算耗尽：由 `_check_budget` 抛出，`chat` 捕获后降级回离线模拟。"""


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量；缺失或非法时回退默认值，避免启动即崩。"""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class OpenAICompatAdapter:
    """默认适配器：OpenAI 兼容 /chat/completions（DeepSeek / GLM / Qwen / Doubao 通用）。

    自定义适配器（SRS FR-M02-009）：继承本类并覆盖 `complete()`，
    实例传给 `ModelGateway(adapter=...)` 即可接管全部在线请求；
    网关的预算、降级与记账逻辑对适配器透明，无需重复实现。
    """

    name = "openai-compat"

    def __init__(self, timeout: int = 60, max_retries: int = 3):
        """timeout 为单次请求超时秒数；max_retries 为限流退避重试次数（0.5s/1.5s/3s）。"""
        self.timeout = timeout
        self.max_retries = max_retries

    def complete(self, *, base_url: str, api_key: str, model: str,
                 system: str, user: str, temperature: float) -> str:
        """发送一轮对话并返回模型文本；重试耗尽后抛异常，由网关统一降级。"""
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
        }).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.max_retries):  # 限流退避：0.5s / 1.5s / 3s
            req = urllib.request.Request(
                f"{base_url}/chat/completions", data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
            except (urllib.error.URLError, urllib.error.HTTPError,
                    KeyError, TimeoutError) as exc:
                last_error = exc
                time.sleep(0.5 * (3 ** attempt))
        raise last_error or RuntimeError("模型调用失败")


class ModelGateway:
    """模型网关：主模型 + 轻量模型两级路由，统一走 OpenAI 兼容接口，
    离线时退化为确定性模板；适配器可替换。

    约定（决定了“可复现”能不能成立）：
    - 所有数字由确定性模块计算，模型只负责归因叙述与语义层 Judge；
    - 模型不可用时整个流程仍能跑完，评测得分数值不变，只有叙述文本变化；
    - 每次调用都记账（token、耗时、模型层级），超过预算由网关熔断降级。
    """

    TIERS = ("primary", "light")

    def __init__(self, token_budget: int = 600_000, model: str | None = None,
                 lite_model: str | None = None,
                 adapter: OpenAICompatAdapter | None = None):
        """读取 GCS_LLM_BASE_URL / GCS_LLM_API_KEY / GCS_LLM_MODEL /
        GCS_LLM_LITE_MODEL 环境变量；URL 与 key 同时非空才判定为在线，
        否则进入 offline-mock。GCS_LLM_TOKEN_BUDGET 可覆盖默认预算（缺省 600,000）。
        lite_model 留空时 `tier="light"` 的调用自动回落主模型。
        adapter 缺省为 `OpenAICompatAdapter`，可注入自定义子类。"""
        self.base_url = os.getenv("GCS_LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("GCS_LLM_API_KEY", "")
        self.model = model or os.getenv("GCS_LLM_MODEL", "deepseek-chat")
        self.lite_model = lite_model or os.getenv("GCS_LLM_LITE_MODEL", "")
        self.adapter = adapter or OpenAICompatAdapter()
        self.token_budget = _env_int("GCS_LLM_TOKEN_BUDGET", token_budget)
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0
        self.latency_ms = 0.0
        self.online = bool(self.base_url and self.api_key)
        self.mode = "online" if self.online else "offline-mock"
        # 分层记账：primary / light 各自记录实际使用的模型与 token 用量
        self.tier_stats: dict[str, dict] = {
            "primary": {"model": "", "calls": 0, "tokens_in": 0, "tokens_out": 0},
            "light": {"model": "", "calls": 0, "tokens_in": 0, "tokens_out": 0},
        }

    # ------------------------------------------------------------------
    def _resolve_model(self, tier: str) -> str:
        """路由规则：light 层配置了轻量模型则用之，否则回落主模型。"""
        if tier == "light" and self.lite_model:
            return self.lite_model
        return self.model

    def chat(self, system: str, user: str, temperature: float = 0.2,
             tier: str = "primary") -> str:
        """发送一轮对话：先检查预算（超限即降级回离线模板，不中断流程），
        在线则按 tier 路由到主/轻量模型（失败则降级），否则走确定性模板；
        每次调用都按层记账 token。tier 可选 "primary"（默认）或 "light"。"""
        if tier not in self.TIERS:
            raise ValueError(f"未知模型层级 {tier!r}（可选 {'/'.join(self.TIERS)}）")
        try:
            self._check_budget()
            exhausted = False
        except BudgetExceeded:
            exhausted = True
            self.mode = "degraded(BudgetExceeded)"

        if exhausted:
            text = self._mock(system, user)
        elif self.online:
            try:
                text = self._request(system, user, temperature,
                                     model=self._resolve_model(tier))
            except Exception as exc:  # 网络/额度异常一律降级，不让演示挂掉
                self.mode = f"degraded({type(exc).__name__})"
                text = self._mock(system, user)
        else:
            text = self._mock(system, user)
        self.calls += 1
        self.tokens_in += _count(system + user)
        self.tokens_out += _count(text)
        ts = self.tier_stats[tier]
        ts["model"] = self._resolve_model(tier)
        ts["calls"] += 1
        ts["tokens_in"] += _count(system + user)
        ts["tokens_out"] += _count(text)
        return text

    def _request(self, system: str, user: str, temperature: float,
                 model: str | None = None) -> str:
        """真实请求（在线）：委托给适配器发送并计时；失败由 chat 统一降级。
        保留独立方法便于测试打桩（mock `_request` 即可离线验证路由）。"""
        started = time.time()
        text = self.adapter.complete(
            base_url=self.base_url, api_key=self.api_key,
            model=model or self.model, system=system, user=user,
            temperature=temperature)
        self.latency_ms += (time.time() - started) * 1000
        return text

    def _mock(self, system: str, user: str) -> str:
        """离线确定性模板：叙述类请求复述已算好的事实，不编造数字；判定类（Judge）
        依据“结论与依据是否自洽”返回 PASS / FAIL，保证 L5 在离线时仍可解析。"""
        first = user.strip().splitlines()[0] if user.strip() else ""
        if "PASS" in system or "FAIL" in system:
            # 语义 Judge：离线时依据“结论与依据是否自洽”做规则判定
            ok = ("选定" in first) and ("排除" in first)
            return "PASS" if ok else "FAIL"
        if first.startswith("本批次"):
            return first
        return f"[离线模式] 已按知识包与硬约束完成「{first[:40]}」，结论由确定性模块给出，本段为模板叙述。"

    def _check_budget(self) -> None:
        """预算检查：累计 token 达到预算则抛出 BudgetExceeded；`chat` 会捕获并降级回离线模拟。"""
        if self.tokens_in + self.tokens_out >= self.token_budget:
            raise BudgetExceeded(
                f"token 预算耗尽：{self.tokens_in + self.tokens_out}/{self.token_budget}")

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """返回计账快照：mode / model / model_lite / calls / tokens_in /
        tokens_out / tokens_total / latency_ms / budget / adapter / tier_stats。"""
        return {
            "mode": self.mode,
            "model": self.model,
            "model_lite": self.lite_model,
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_in + self.tokens_out,
            "latency_ms": round(self.latency_ms, 1),
            "budget": self.token_budget,
            "adapter": self.adapter.name,
            "tier_stats": {
                tier: dict(s) for tier, s in self.tier_stats.items()},
        }


def _count(text: str) -> int:
    """粗估 token：中文约 1 字 1 token，英文约 4 字符 1 token，取保守值。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4 + 1
