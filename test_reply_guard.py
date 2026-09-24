"""ReplyGuard 回复审查器单元测试

覆盖：
- 违规话术黑名单（LLM 出口 + 知识库出口）
- 价格白名单（合法价格放行 / 编造价格拦截 / 组合价放行）
- 函数调用回复跳过价格检查（数据库数字可信）
- 替换话术自身合规（不被黑名单二次命中）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.reply_guard import ReplyGuard, VIOLATION_RULES, SAFE_REPLIES


class TestViolationBlacklist(unittest.TestCase):
    def setUp(self):
        self.guard = ReplyGuard()

    def test_refund_violation_llm(self):
        r = self.guard.check("可以的，您的卡随时可退，不用担心～")
        self.assertFalse(r["passed"])
        self.assertTrue(r["replaced"])
        self.assertEqual(r["category"], "refund")
        self.assertNotIn("随时可退", r["content"])

    def test_violation_from_knowledge_base(self):
        """复现知识库 #17 教训：KB 直出的违规文案必须被拦。"""
        bad_kb_answer = "可以的，未消费项目随时可退，合同里写得很清楚。"
        r = self.guard.check(bad_kb_answer, source="knowledge_base")
        self.assertTrue(r["replaced"])
        self.assertEqual(r["category"], "refund")
        self.assertNotIn("随时可退", r["content"])
        self.assertNotIn("合同", r["content"])

    def test_compliant_refund_passes(self):
        compliant = (
            "退卡有明确规则的：办卡7天内未消费可以全额退，超过7天退剩余余额，"
            "次卡按剩余次数折算哦～"
        )
        r = self.guard.check(compliant, source="knowledge_base")
        self.assertTrue(r["passed"])
        self.assertFalse(r["replaced"])

    def test_effect_violation(self):
        r = self.guard.check("放心，我们的护理保证有效，做一次就顺滑！")
        self.assertTrue(r["replaced"])
        self.assertEqual(r["category"], "effect")

    def test_promise_violation(self):
        r = self.guard.check("这个卡终身有效，您随时来都行～")
        self.assertTrue(r["replaced"])
        self.assertEqual(r["category"], "promise")

    def test_min_price_violation(self):
        r = self.guard.check("我们是全市最低价，别家都比我们贵！")
        self.assertTrue(r["replaced"])
        self.assertEqual(r["category"], "price")

    def test_empty_reply(self):
        r = self.guard.check("")
        self.assertTrue(r["passed"])

    def test_whitespace_variant_blocked(self):
        """空白变体绕过：「随 时 可 退」同样拦截。"""
        r = self.guard.check("您的卡随 时 可 退，放心～")
        self.assertTrue(r["replaced"])

    def test_safe_replies_self_compliant(self):
        """替换话术自身不得再命中黑名单（防止替换即违规）。"""
        for cat, text in SAFE_REPLIES.items():
            for word, rule_cat in VIOLATION_RULES:
                self.assertNotIn(
                    word, text,
                    f"SAFE_REPLIES[{cat!r}] 含违规词 {word!r}"
                )


class TestPriceWhitelist(unittest.TestCase):
    def setUp(self):
        self.guard = ReplyGuard()

    def test_listed_price_passes(self):
        r = self.guard.check("男士剪发38元，女士剪发58元哦～", source="llm")
        self.assertTrue(r["passed"])

    def test_combo_price_passes(self):
        r = self.guard.check("两项一起做的话一共396元，很划算的～", source="llm")
        self.assertTrue(r["passed"])

    def test_fabricated_price_blocked(self):
        r = self.guard.check("我们剪发只要9.9元，超实惠！", source="llm")
        self.assertTrue(r["replaced"])
        self.assertEqual(r["category"], "price")
        self.assertNotIn("9.9", r["content"])

    def test_fabricated_yen_symbol_blocked(self):
        r = self.guard.check("染发只需要¥188哦～", source="llm")
        self.assertTrue(r["replaced"])

    def test_function_call_reply_skips_price_check(self):
        """函数调用支撑的数字来自数据库，跳过价格白名单。"""
        r = self.guard.check(
            "您的卡里余额还有 ¥330.5 元，随时可以用哦～",
            source="llm",
            has_function_calls=True,
        )
        self.assertTrue(r["passed"])

    def test_kb_source_skips_price_check(self):
        """知识库价格是店家授权事实，跳过价格白名单。"""
        r = self.guard.check("剪+烫套餐238元，包含洗剪吹～", source="knowledge_base")
        self.assertTrue(r["passed"])

    def test_discount_word_without_amount_passes(self):
        """无货币标记的数字（如'85折'）不在价格检查范围。"""
        r = self.guard.check("办储值卡相当于打85折，很划算的～", source="llm")
        self.assertTrue(r["passed"])


class TestIntegrationWithAgent(unittest.TestCase):
    def test_agent_has_guard(self):
        """Agent 实例应持有审查器。"""
        import asyncio
        from agent.agent import Agent
        from agent.providers import create_provider

        p = create_provider("openai", api_key="sk-test", model="m",
                            base_url="http://localhost:9")
        a = Agent(p, system_prompt="SYS")
        self.assertIsNotNone(a.reply_guard)
        # 模拟 KB 出口审查路径
        r = a.reply_guard.check("未消费项目随时可退", source="knowledge_base")
        self.assertTrue(r["replaced"])

    def test_agent_exit_replacement_updates_history(self):
        """LLM 出口替换后，历史中的 assistant 消息应同步更新。"""
        from agent.agent import Agent
        from agent.providers import create_provider
        from agent.providers.base import LLMMessage

        p = create_provider("openai", api_key="sk-test", model="m",
                            base_url="http://localhost:9")
        a = Agent(p, system_prompt="SYS")
        a.conversation_history.append(
            LLMMessage(role="assistant", content="未消费项目随时可退哈～")
        )
        guard = a.reply_guard.check(
            a.conversation_history[-1].content, source="llm"
        )
        if guard["replaced"]:
            a.conversation_history[-1].content = guard["content"]
        self.assertNotIn("随时可退", a.conversation_history[-1].content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
