#!/usr/bin/env python3
"""历史裁剪回归（S3 收口场景固化）：正常裁剪/工具对完整/硬裁分支。

运行: python test_history_trimming.py
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


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


class SeqProvider(LLMProvider):
    """按脚本返回：含 function_calls 的多轮。"""

    def __init__(self, script):
        self.script = script
        self.i = 0

    @property
    def model_name(self):
        return "seq-test"

    async def chat(self, messages, functions=None, **kwargs):
        r = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return r

    def supports_function_calling(self):
        return True


class FC:
    def __init__(self, fid, name):
        self.id, self.name = fid, name
        self.arguments = {}


def make_agent(script):
    p = SeqProvider(script)
    return Agent(p, system_prompt="S"), p


async def test_normal_trimming():
    print("\n▶ 正常裁剪：12轮后裁到窗口内")
    agent, _ = make_agent([LLMResponse(content="ok")])
    agent.max_history_messages = 10
    for i in range(12):
        await agent.chat(f"m{i}", session_id="t")
    h = agent._get_history("t")
    check("历史被裁剪", len(h) < 25, f"len={len(h)}")
    check("保留最新消息", h[-1].role == "assistant")
    check("system仍在开头", h[0].role == "system")
    starts = [i for i, m in enumerate(h) if i > 0 and m.role == "user"]
    check("裁剪后仍是user开头轮次结构", bool(starts))


async def test_tool_pair_integrity():
    print("\n▶ 工具对完整：assistant(tool_calls)+tool 不被拆散")
    # 脚本：第1次返回带 function_calls，第2次返回最终文本，之后纯文本
    agent, p = make_agent([
        LLMResponse(content="", function_calls=[FC("c1", "f1")]),
        LLMResponse(content="done"),
        LLMResponse(content="final"),
    ])
    agent.use_knowledge_base = False
    agent.max_history_messages = 4  # 触发激进裁剪
    for i in range(6):
        await agent.chat(f"轮{i}", session_id="tt")
    h = agent._get_history("tt")
    # 断言：任何 assistant(tool_calls) 后面紧跟的必须是同 tool_call_id 的 tool
    intact = True
    for idx, m in enumerate(h):
        if getattr(m, "tool_calls", None):
            nxt = h[idx + 1] if idx + 1 < len(h) else None
            if not (nxt and nxt.role == "tool" and nxt.tool_call_id == m.tool_calls[0].id):
                intact = False
    check("tool_calls配对未被拆散", intact)
    starts = [i for i, m in enumerate(h) if i > 0 and m.role == "user"]
    check("system后紧跟user", h[1].role == "user")


async def test_hard_cut():
    print("\n▶ 硬裁分支：无user轮时保命裁剪")
    agent, _ = make_agent([LLMResponse(content="x")])
    agent._histories["hard"] = [LLMMessage(role="system", content="S")]
    h = agent._get_history("hard")
    # 伪造 16 条无 user 的 assistant 历史
    for i in range(16):
        h.append(LLMMessage(role="assistant", content=f"a{i}"))
    agent.max_history_messages = 10
    agent._trim_history(h)
    check("硬裁后 system+10", len(h) == 11, f"len={len(h)}")
    check("保留最近消息", h[-1].content == "a15")
    check("system保留", h[0].role == "system")


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            asyncio.run(f())
    print(f"\n{'='*50}\n历史裁剪测试: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
