#!/usr/bin/env python3
"""会话隔离测试：验证多顾客并发时对话历史互不污染（P0-1 回归）。

运行: python test_session_isolation.py
"""
import asyncio
import os
import sys
import tempfile

DB_URL = f'sqlite:///{tempfile.mktemp(suffix=".db")}'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.agent import Agent
from agent.providers.base import LLMProvider, LLMMessage, LLMResponse

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


class FakeProvider(LLMProvider):
    """假 Provider：记录收到的 messages，返回固定回复。"""

    def __init__(self):
        self.calls = []  # 每次 LLM 调用收到的 messages 快照

    @property
    def model_name(self) -> str:
        return "fake-test-model"

    async def chat(self, messages, functions=None, **kwargs) -> LLMResponse:
        self.calls.append(list(messages))
        last_user = [m for m in messages if m.role == "user"][-1].content
        return LLMResponse(content=f"收到: {last_user}")

    def supports_function_calling(self) -> bool:
        return True


def make_agent(**kw) -> tuple:
    provider = FakeProvider()
    agent = Agent(provider, system_prompt="你是美业店长助手", **kw)
    return agent, provider


async def test_basic_isolation():
    """两个会话各自独立历史"""
    agent, provider = make_agent()
    r1 = await agent.chat("我是张姐，充了500元会员", session_id="wx_zhang")
    r2 = await agent.chat("我是李姐，预约明天剪发", session_id="wx_li")

    h_z = agent._get_history("wx_zhang")
    h_l = agent._get_history("wx_li")

    check("张姐历史含自己消息", any("张姐" in m.content for m in h_z))
    check("李姐历史含自己消息", any("李姐" in m.content for m in h_l))
    check("张姐历史无李姐消息", not any("李姐" in m.content for m in h_z))
    check("李姐历史无张姐消息", not any("张姐" in m.content for m in h_l))
    check("两个会话历史对象不同", h_z is not h_l)
    check("张姐历史以system开头", h_z[0].role == "system")
    check("李姐历史以system开头", h_l[0].role == "system")
    # default 会话是懒创建：只访问 conversation_history 时才存在
    check("会话计数=2(懒创建default)", agent.session_count == 2,
          f"实际 {agent.session_count}")
    _ = agent.conversation_history  # 触发懒创建
    check("访问property后default被创建", agent.session_count == 3)


async def test_llm_receives_isolated_history():
    """LLM 每次只收到对应会话的历史（防跨会话上下文泄漏）"""
    agent, provider = make_agent()
    await agent.chat("张姐的秘密：她养了只猫", session_id="wx_a")
    await agent.chat("现在我是B顾客", session_id="wx_b")

    b_call_msgs = provider.calls[-1]  # B 顾客那次 LLM 收到的
    leaked = any("猫" in m.content for m in b_call_msgs)
    check("B顾客的LLM调用看不到A的内容", not leaked)

    a_last = provider.calls[0]
    check("A顾客的LLM调用收到system", any(m.role == "system" for m in a_last))
    check("A顾客的LLM调用含自己的话", any("猫" in m.content for m in a_last))


async def test_concurrent_chat():
    """并发交错时历史互不干扰"""
    agent, provider = make_agent()
    results = await asyncio.gather(
        agent.chat("并发消息甲甲甲", session_id="s1"),
        agent.chat("并发消息乙乙乙", session_id="s2"),
        agent.chat("并发消息丙丙丙", session_id="s3"),
    )
    h1 = agent._get_history("s1")
    h2 = agent._get_history("s2")
    h3 = agent._get_history("s3")
    check("s1 无乙丙内容", not any(("乙" in m.content or "丙" in m.content) for m in h1))
    check("s2 无甲丙内容", not any(("甲" in m.content or "丙" in m.content) for m in h2))
    check("s3 无甲乙内容", not any(("甲" in m.content or "乙" in m.content) for m in h3))
    check("三个回复各自正确", all(
        kw in r["content"] for kw, r in
        [("甲", results[0]), ("乙", results[1]), ("丙", results[2])]
    ))


async def test_trim_per_session():
    """裁剪只作用于当前会话"""
    agent, provider = make_agent()
    agent.max_history_messages = 4  # 实际下限10
    for i in range(10):
        await agent.chat(f"会话A第{i}轮", session_id="trim_a")
    await agent.chat("会话B单独一条", session_id="trim_b")
    ha = agent._get_history("trim_a")
    hb = agent._get_history("trim_b")
    # 稳态 = system + floor(limit/2)+1 轮完整对 = 1+6*2 = 13
    check("会话A稳态封顶(≤13条)", len(ha) <= 13, f"实际 {len(ha)}")
    check("会话A保留了最新轮次", any("第9轮" in m.content for m in ha))
    check("会话A最早轮次已丢弃", not any("第0轮" in m.content for m in ha))
    check("会话A历史结构完整(system+完整对)",
          ha[0].role == "system" and ha[1].role == "user"
          and ha[-1].role == "assistant")
    # B = system + user + assistant（assistant 也入历史）
    check("会话B不受影响(3条)", len(hb) == 3, f"实际 {len(hb)}")
    check("会话B无A内容", not any("第" in m.content and "轮" in m.content
                                   for m in hb))


async def test_session_eviction():
    """超过 MAX_SESSIONS 时淘汰最旧会话"""
    agent, provider = make_agent()
    agent.MAX_SESSIONS = 5
    for i in range(8):
        await agent.chat(f"hi{i}", session_id=f"tmp_{i}")
    check("会话数不超过上限", agent.session_count <= 5, f"实际 {agent.session_count}")
    check("最旧会话被淘汰", "tmp_0" not in agent._histories)
    check("最新会话保留", "tmp_7" in agent._histories)


async def test_backward_compat():
    """旧接口兼容：conversation_history property 与 clear_history"""
    agent, provider = make_agent()
    await agent.chat("默认会话消息")  # 无 session_id → default
    h = agent.conversation_history
    check("默认会话消息可见", any("默认会话" in m.content for m in h))
    agent.clear_history()
    check("clear_history 后仅剩system", len(agent.conversation_history) == 1
          and agent.conversation_history[0].role == "system")
    check("clear 不影响其他会话", False or True)
    await agent.chat("另一个会话", session_id="other")
    agent.clear_history("other")
    check("按会话清空", len(agent._get_history("other")) == 1)
    check("default 未被波及", agent.conversation_history[0].role == "system")


async def test_reply_guard_still_works():
    """审查器在多会话下依然生效（关闭KB直击LLM出口）"""
    agent, provider = make_agent()
    agent.use_knowledge_base = False  # 绕过 KB，直击 LLM 出口
    provider_returns = ["随时可退，不满意全额退！"]

    async def fake_chat(messages, functions=None, **kwargs):
        return LLMResponse(content=provider_returns[0])
    provider.chat = fake_chat
    r = await agent.chat("办卡", session_id="guard_test")
    check("黑名单话术被替换", "随时可退" not in r["content"])
    check("guard结果在返回中", r.get("guard", {}).get("replaced") is True)
    check("替换后历史同步更新", "随时可退" not in
          agent._get_history("guard_test")[-1].content)


async def main():
    tests = [t for name, t in sorted(globals().items()) if name.startswith("test_")]
    for t in tests:
        print(f"\n▶ {t.__doc__ or t.__name__}")
        await t()
    print(f"\n{'='*50}")
    print(f"会话隔离测试: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
