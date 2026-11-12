"""模型网关的单元测试：离线可复现、判定可解析、预算可熔断。

这三条决定了方案书里那句“模型缺席时结论不变”是否站得住：
- 离线模式必须是确定性的（同样输入两次调用完全一致）；
- 叙述文本不能出现输入里没有的数字（否则等于模型在编数据）；
- 语义 Judge 必须真的返回 PASS / FAIL，否则 L5 层失效。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.llm import BudgetExceeded, ModelGateway, _count  # noqa: E402

OFFLINE_ENV = {"GCS_LLM_BASE_URL": "", "GCS_LLM_API_KEY": "", "GCS_LLM_MODEL": ""}
JUDGE_SYS = "你是调度决策审核员。判断决策依据是否自洽，只回答 PASS 或 FAIL。"


class TestOfflineMode(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", OFFLINE_ENV)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.llm = ModelGateway()

    def test_without_credentials_it_runs_in_mock_mode(self):
        self.assertFalse(self.llm.online)
        self.assertEqual(self.llm.mode, "offline-mock")
        self.assertFalse(self.llm.model, "未设置 GCS_LLM_MODEL 时 model 应为空串")

    def test_mock_is_deterministic(self):
        a = self.llm.chat("撰写报告", "本批次 20 个作业，平均等待 0.25 分钟。")
        b = self.llm.chat("撰写报告", "本批次 20 个作业，平均等待 0.25 分钟。")
        self.assertEqual(a, b)

    def test_narrative_reuses_supplied_facts_verbatim(self):
        fact = "本批次共 20 个作业，预算达成率 100.0%，平均单位成本 2.49 元/GPU·h。"
        self.assertEqual(self.llm.chat("撰写报告", fact), fact)

    def test_narrative_does_not_invent_numbers(self):
        fact = "本批次 20 个作业，等待 7.88 分钟。"
        out = self.llm.chat("撰写报告", fact)
        # 输出里出现的数字必须都能在输入里找到，不能凭空产生指标
        digits_in = {ch for ch in fact if ch.isdigit()}
        self.assertTrue({ch for ch in out if ch.isdigit()} <= digits_in, out)


class TestSemanticJudge(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", OFFLINE_ENV)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.llm = ModelGateway()

    def test_pass_when_both_choice_and_exclusion_given(self):
        out = self.llm.chat(JUDGE_SYS, "决策：ZW-A100 @ 120min；依据：选定 ZW-A100，排除更便宜的 QD-A100。")
        self.assertEqual(out, "PASS")

    def test_fail_when_exclusion_reason_missing(self):
        out = self.llm.chat(JUDGE_SYS, "决策：ZW-A100 @ 120min；依据：选定 ZW-A100。")
        self.assertEqual(out, "FAIL")

    def test_fail_when_decision_text_is_bare(self):
        out = self.llm.chat(JUDGE_SYS, "决策：ZW-A100 @ 120min")
        self.assertEqual(out, "FAIL")

    def test_verifier_can_parse_the_verdict(self):
        out = self.llm.chat(JUDGE_SYS, "选定 X，排除更绿的 Y。")
        self.assertIn("PASS", out.upper())  # 验证器用的就是这个判据


class TestAccountingAndBudget(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", OFFLINE_ENV)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_stats_track_calls_and_tokens(self):
        llm = ModelGateway()
        for i in range(3):
            llm.chat("撰写报告", f"本批次第 {i} 段事实。")
        st = llm.stats()
        self.assertEqual(st["calls"], 3)
        self.assertGreater(st["tokens_total"], 0)
        self.assertEqual(st["tokens_total"], st["tokens_in"] + st["tokens_out"])
        self.assertEqual(st["budget"], 600_000)
        self.assertEqual(st["mode"], "offline-mock")

    def test_budget_exhaustion_degrades_instead_of_crashing(self):
        """预算耗尽是降级条件而非致命错误：chat 必须返回模板文本并标记 degraded。"""
        llm = ModelGateway(token_budget=1)
        llm.chat("撰写报告", "本批次一句话。")  # 这一次把预算用光
        text = llm.chat("撰写报告", "再看一句。")
        self.assertTrue(text)
        self.assertEqual(llm.mode, "degraded(BudgetExceeded)")

    def test_budget_message_reports_usage(self):
        """`_check_budget` 仍负责给出可读的用量信息，供日志与告警使用。"""
        llm = ModelGateway(token_budget=1)
        llm.chat("撰写报告", "本批次一句话。")
        with self.assertRaises(BudgetExceeded) as ctx:
            llm._check_budget()
        self.assertIn("/1", str(ctx.exception))

    def test_budget_exhaustion_degrades_instead_of_crashing(self):
        """预算耗尽是降级条件而非致命错误：chat 必须返回模板文本并标记 degraded。"""
        llm = ModelGateway(token_budget=1)
        llm.chat("撰写报告", "本批次一句话。")  # 这一次把预算用光
        text = llm.chat("撰写报告", "再看一句。")
        self.assertTrue(text)
        self.assertEqual(llm.mode, "degraded(BudgetExceeded)")

    def test_token_budget_env_override(self):
        """GCS_LLM_TOKEN_BUDGET 必须真正生效，避免成为 .env.example 里的孤儿配置。"""
        with mock.patch.dict("os.environ", {"GCS_LLM_TOKEN_BUDGET": "42"}, clear=False):
            llm = ModelGateway()
        self.assertEqual(llm.token_budget, 42)
        self.assertEqual(llm.stats()["budget"], 42)

    def test_cjk_costs_more_tokens_than_ascii(self):
        self.assertGreater(_count("四个汉字"), _count("abcd"))


class TestDegradation(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", OFFLINE_ENV)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_request_failure_degrades_to_mock(self):
        llm = ModelGateway()
        llm.online = True  # 假装有凭证，但请求必然失败
        with mock.patch.object(llm, "_request", side_effect=RuntimeError("boom")):
            out = llm.chat("撰写报告", "本批次 20 个作业。")
        self.assertTrue(llm.mode.startswith("degraded("))
        self.assertIn("本批次 20 个作业", out)

    def test_online_flag_requires_both_url_and_key(self):
        with mock.patch.dict("os.environ", {"GCS_LLM_BASE_URL": "https://api.deepseek.com/v1/",
                                            "GCS_LLM_API_KEY": ""}):
            self.assertFalse(ModelGateway().online, "只有地址没有 key 不应判为在线")
        with mock.patch.dict("os.environ", {"GCS_LLM_BASE_URL": "https://api.deepseek.com/v1/",
                                            "GCS_LLM_API_KEY": "sk-test"}):
            llm = ModelGateway()
            self.assertTrue(llm.online)
            self.assertEqual(llm.base_url, "https://api.deepseek.com/v1", "末尾斜杠应被去掉")


if __name__ == "__main__":
    unittest.main()
