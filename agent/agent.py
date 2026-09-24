"""Agent 核心类 - 整合 LLM 和函数调用。

本模块实现了 Agent 核心类，负责整合大语言模型（LLM）提供商和函数调用
机制，提供统一的对话接口。Agent 支持多轮对话、函数调用和自动迭代处理。

对用户透明的多模型支持：
    Agent 通过 LLMProvider 抽象接口与底层模型交互，用户无需关心
    使用的是 OpenAI、Claude 还是 MiniMax，切换模型只需更换 Provider。

函数调用流程：
    1. 用户发送消息 → Agent 调用 Provider
    2. Provider 返回 LLMResponse（可能包含 function_calls）
    3. Agent 存储 assistant 消息（包含 tool_calls 和 provider_extras）
    4. Agent 执行每个函数调用，存储 tool 消息（包含 tool_call_id）
    5. 重复 2-4 直到 LLM 给出最终回复或达到最大迭代次数

多会话隔离（2026-09-18）：
    chat() 接受 session_id 参数，每个会话拥有独立对话历史。
    微信渠道每个顾客 session_id 不同（wx_{openid}），杜绝跨顾客
    上下文污染。不传 session_id 时使用默认会话，向后兼容。
"""
from typing import List, Dict, Any, Optional, Callable
from loguru import logger

# 转人工意图关键词：AI回复中出现即视为需要店主人工介入。
# 覆盖两类信号：(1)主动转接类 (2)把决策/授权推回店主类（含审查器安全话术）。
HUMAN_HANDOFF_KEYWORDS = [
    "转人工", "转接店主", "联系店主", "找店主", "店主帮您", "店主亲自",
    "联系店长", "找店长", "店长帮您", "转店长", "问问店长", "问店长",
    "店主确认", "跟店主确认", "问店主", "让店主", "店主说了算", "得店主",
    "到店详谈", "到店面谈", "面诊", "线下沟通", "私信店主", "加店主微信",
]


def detect_needs_human(content: str) -> bool:
    """检测AI回复是否需要店主人工介入。"""
    if not content:
        return False
    return any(k in content for k in HUMAN_HANDOFF_KEYWORDS)

from agent.providers.base import LLMProvider, LLMMessage, LLMResponse

# 知识库懒加载（避免循环导入）
_knowledge_base = None


def get_knowledge_base():
    """获取知识库单例（懒加载）"""
    global _knowledge_base
    if _knowledge_base is None:
        try:
            from config.knowledge_base import KnowledgeBase
            _knowledge_base = KnowledgeBase()
            logger.info(f"知识库加载完成: {_knowledge_base.get_stats()}")
        except Exception as e:
            logger.warning(f"知识库加载失败，将跳过知识库匹配: {e}")
            _knowledge_base = None
    return _knowledge_base
from agent.functions.registry import FunctionRegistry
from agent.functions.executor import ToolExecutor


class Agent:
    """Agent 核心类，整合 LLM 提供商和函数调用机制。

    Agent 提供统一的对话接口，对用户透明地支持不同的模型提供商。
    当 LLM 决定调用函数时，Agent 自动执行并将结果返回给 LLM，
    实现多轮迭代直到获得最终回复。

    关键设计：
        - tool_calls 和 tool_call_id 的正确跟踪，确保多轮工具调用
          在所有提供商上都能正确工作
        - provider_extras 透传机制，让 Anthropic 系列提供商能在
          多轮对话中保持完整的上下文（包括 thinking 块等）
        - 多会话历史隔离：session_id → 独立历史列表，多顾客并发
          互不污染

    Attributes:
        provider: LLM 提供商实例。
        function_registry: 函数注册表。
        tool_executor: 工具执行器。
        system_prompt: 系统提示词。
        max_sessions: 会话历史上限（防内存无限增长）。

    Example:
        ```python
        from agent import Agent, create_provider, FunctionRegistry

        provider = create_provider("openai", api_key="sk-...")
        registry = FunctionRegistry()
        agent = Agent(provider, registry, system_prompt="你是一个助手")

        response = await agent.chat("查询用户信息", session_id="wx_abc")
        print(response["content"])
        ```
    """

    # 会话历史上限：超过后清理最久未活跃的会话，防内存泄漏
    MAX_SESSIONS: int = 200

    def __init__(
        self,
        provider: LLMProvider,
        function_registry: Optional[FunctionRegistry] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        """初始化 Agent 实例。

        Args:
            provider: LLM 提供商实例，必须实现 LLMProvider 接口。
            function_registry: 函数注册表。如果为 None，创建空注册表。
            system_prompt: 系统提示词，设置 Agent 的行为和角色。
        """
        self.provider = provider
        self.function_registry = function_registry or FunctionRegistry()
        self.tool_executor = ToolExecutor(self.function_registry)
        self.system_prompt = system_prompt
        self.use_knowledge_base: bool = True  # 知识库开关
        # 历史滑动窗口：超出上限时按轮裁剪（防止长对话 token 膨胀和上下文污染）
        self.max_history_messages: int = 24
        # 多会话隔离：session_id → 独立对话历史
        self._histories: Dict[str, List[LLMMessage]] = {}
        # 回复审查器：发送前的确定性安全闸门（违规话术黑名单 + 价格白名单）
        from agent.reply_guard import get_reply_guard
        self.reply_guard = get_reply_guard()

    def _get_history(self, session_id: str) -> List[LLMMessage]:
        """获取指定会话的历史列表，不存在则创建（以 system 提示词开头）。

        Args:
            session_id: 会话标识。空值归入默认会话。

        Returns:
            该会话的对话历史列表（就地修改生效）。
        """
        key = session_id or "default"
        history = self._histories.get(key)
        if history is None:
            # 会话数量治理：超上限时淘汰最旧的会话（dict 保持插入序）
            if len(self._histories) >= self.MAX_SESSIONS:
                oldest = next(iter(self._histories))
                self._histories.pop(oldest, None)
                logger.info(f"会话历史上限({self.MAX_SESSIONS})，已淘汰: {oldest}")
            history = []
            if self.system_prompt:
                history.append(
                    LLMMessage(role="system", content=self.system_prompt)
                )
            self._histories[key] = history
            logger.debug(f"新会话历史已创建: {key}")
        return history

    @property
    def conversation_history(self) -> List[LLMMessage]:
        """向后兼容：返回默认会话的历史。"""
        return self._get_history("default")

    @conversation_history.setter
    def conversation_history(self, value: List[LLMMessage]) -> None:
        """向后兼容：允许整体替换默认会话历史（旧代码 clear 模式）。"""
        self._histories["default"] = value

    @property
    def session_count(self) -> int:
        """当前活跃会话数。"""
        return len(self._histories)

    async def chat(
        self,
        user_message: str,
        max_iterations: int = 10,
        session_id: str = "default",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """与 Agent 进行对话（按 session_id 隔离对话历史）。

        支持多轮迭代：如果 LLM 决定调用函数，Agent 会执行函数并将
        结果返回给 LLM，LLM 可以基于结果继续处理或调用更多函数，
        直到获得最终回复。

        Args:
            user_message: 用户输入的消息内容。
            max_iterations: 最大迭代次数，默认 10。
            session_id: 会话标识。不同顾客/渠道传不同值，
                对话历史互相隔离；缺省为默认会话。
            **kwargs: 传递给 LLM 提供商的额外参数。

        Returns:
            包含以下键的字典：
                - content (str): LLM 的最终回复内容。
                - function_calls (List[Dict]): 所有函数调用记录。
                - iterations (int): 实际迭代次数。
        """
        history = self._get_history(session_id)

        # 添加用户消息到对话历史
        history.append(
            LLMMessage(role="user", content=user_message)
        )
        self._trim_history(history)

        # ====== 知识库匹配（优先于 LLM）======
        if self.use_knowledge_base:
            kb = get_knowledge_base()
            if kb:
                kb_result = kb.lookup(user_message)
                if kb_result:
                    logger.info(
                        f"知识库命中 [{kb_result['category']}]: {kb_result['question']}"
                    )
                    # 知识库直出同样过审查器：KB 文案错误是真实事故源
                    guard = self.reply_guard.check(
                        kb_result["answer"], source="knowledge_base"
                    )
                    final_content = guard["content"]
                    if guard["replaced"]:
                        logger.warning(
                            f"[ReplyGuard] KB条目命中黑名单已替换: "
                            f"{kb_result['question']!r} rule={guard['rule']!r}"
                        )
                    # 将最终回答添加为 assistant 消息（历史与实际发送保持一致）
                    history.append(
                        LLMMessage(role="assistant", content=final_content)
                    )
                    return {
                        "content": final_content,
                        "function_calls": [],
                        "iterations": 0,
                        "source": f"knowledge_base:{kb_result['category']}",
                        "guard": guard,
                    }

        # 未命中知识库，走 LLM + function calling

        iterations: int = 0
        function_calls_made: List[Dict[str, Any]] = []
        response: Optional[LLMResponse] = None

        # 迭代处理：支持多轮函数调用
        while iterations < max_iterations:
            iterations += 1

            # 准备函数定义列表（如果支持函数调用且有注册函数）
            functions: Optional[List[Dict[str, Any]]] = None
            if (self.function_registry
                    and self.provider.supports_function_calling()):
                func_list = self.function_registry.list_functions()
                if func_list:
                    functions = func_list

            # 调用 LLM 提供商获取回复（只传当前会话的历史）
            response = await self.provider.chat(
                messages=history,
                functions=functions,
                **kwargs,
            )

            # 将 assistant 回复添加到对话历史
            # 关键：保留 tool_calls 和 provider_extras，确保
            # Provider 在下一轮请求中能恢复完整上下文
            assistant_msg = LLMMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.function_calls,
                provider_extras=response.raw_response,
            )
            history.append(assistant_msg)

            # 如果没有函数调用，审查后返回最终回复
            if not response.function_calls:
                guard = self.reply_guard.check(
                    response.content,
                    source="llm",
                    has_function_calls=bool(function_calls_made),
                )
                if guard["replaced"]:
                    # 历史同步替换，避免下一轮 LLM 从违规话术中模仿
                    assistant_msg.content = guard["content"]
                return {
                    "content": guard["content"],
                    "needs_human": detect_needs_human(guard["content"]),
                    "function_calls": function_calls_made,
                    "iterations": iterations,
                    "guard": guard,
                }

            # 处理函数调用：执行每个函数并将结果返回给 LLM
            for func_call in response.function_calls:
                # 记录函数调用信息
                function_calls_made.append({
                    "name": func_call.name,
                    "arguments": func_call.arguments,
                })

                try:
                    # 执行函数调用
                    result: Any = await self.tool_executor.execute(
                        func_call.name, func_call.arguments
                    )

                    # 格式化函数执行结果
                    result_str: str = self.tool_executor.format_result(result)

                    # 将函数结果添加到对话历史
                    # 使用 role="tool" + tool_call_id 关联调用和结果
                    history.append(
                        LLMMessage(
                            role="tool",
                            content=result_str,
                            name=func_call.name,
                            tool_call_id=func_call.id,
                        )
                    )

                except Exception as e:
                    # 函数执行失败，记录错误到对话历史
                    logger.error(
                        f"Error executing function {func_call.name}: {e}"
                    )
                    history.append(
                        LLMMessage(
                            role="tool",
                            content=f"错误: {str(e)}",
                            name=func_call.name,
                            tool_call_id=func_call.id,
                        )
                    )

            # 继续循环，让 LLM 基于函数结果继续处理

        # 达到最大迭代次数
        logger.warning(f"Reached max iterations ({max_iterations})")
        final = response.content if response else ""
        guard = self.reply_guard.check(
            final, source="llm", has_function_calls=bool(function_calls_made)
        )
        return {
            "content": guard["content"],
            "needs_human": detect_needs_human(guard["content"]),
            "function_calls": function_calls_made,
            "iterations": iterations,
            "guard": guard,
        }

    async def parse_message(
        self,
        sender: str,
        timestamp: str,
        content: str,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """解析消息并提取结构化数据（兼容原有接口）。

        此方法用于从非结构化消息中提取结构化数据，LLM 会分析消息内容
        并返回 JSON 格式的结构化数据。kwargs 中的 session_id 会透传
        给 chat 方法，实现会话隔离。

        Args:
            sender: 消息发送者。
            timestamp: 消息时间戳。
            content: 消息文本内容。
            **kwargs: 传递给 chat 方法的额外参数。

        Returns:
            解析后的结构化数据列表。
        """
        # 构建解析提示词
        user_prompt: str = f"""消息发送者: {sender}
消息时间: {timestamp}
消息内容:
{content}

请提取结构化数据。返回 JSON 数组格式。"""

        # 调用 chat 方法获取 LLM 回复
        response: Dict[str, Any] = await self.chat(user_prompt, **kwargs)

        # 解析 JSON 响应
        import json
        import re

        content_text: str = response["content"]

        # 清理 Markdown code block
        content_text = content_text.strip()
        if content_text.startswith("```"):
            content_text = re.sub(r'^```(?:json)?\s*', '', content_text)
            content_text = re.sub(r'\s*```$', '', content_text)

        try:
            data: Any = json.loads(content_text)

            if isinstance(data, dict):
                if "records" in data:
                    return data["records"]
                return [data]
            elif isinstance(data, list):
                return data
            else:
                logger.warning(f"Unexpected response format: {type(data)}")
                return [{"type": "noise"}]
        except json.JSONDecodeError as e:
            logger.error(
                f"JSON parse error: {e}, text: {content_text[:200]}"
            )
            return [{"type": "noise", "error": str(e)}]

    def _trim_history(self, history: Optional[List[LLMMessage]] = None) -> None:
        """滑动窗口裁剪：超出 max_history_messages 时按轮丢弃最老的轮次。
        裁剪点只落在 user 消息（每轮起点）上，保证 assistant(tool_calls)/tool
        消息对不被拆散，且 system 之后永远是 user 开头。"""
        if history is None:
            history = self.conversation_history  # 向后兼容
        limit = max(int(self.max_history_messages), 10)
        h = history
        if len(h) <= limit + 1:
            return
        # 收集每轮起点（system 之后的 user 消息 index）
        starts = [
            i for i, m in enumerate(h)
            if i > 0 and getattr(m, "role", None) == "user"
        ]
        if not starts:
            # 异常场景（无任何 user 轮）：硬裁保命，只留 system + 最近 limit 条
            del h[1:max(1, len(h) - limit)]
            return
        # 保留尽可能多的轮次：找最小的起点 s，使 h[s:] 长度 <= limit+1
        keep_from = starts[-1]  # 兜底至少保留最后一轮
        for s in starts:
            if len(h) - s <= limit + 1:
                keep_from = s
                break
        del h[1:keep_from]

    def clear_history(self, session_id: str = "default") -> None:
        """清空指定会话的对话历史，保留系统提示词。

        Args:
            session_id: 会话标识，默认清空默认会话。
        """
        self._histories[session_id or "default"] = self._new_history()

    def _new_history(self) -> List[LLMMessage]:
        """构造一份以 system 提示词开头的新历史。"""
        history: List[LLMMessage] = []
        if self.system_prompt:
            history.append(
                LLMMessage(role="system", content=self.system_prompt)
            )
        return history

    def register_function(
        self,
        name: str,
        description: str,
        func: Callable[..., Any],
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册函数到函数注册表（便捷方法）。

        Args:
            name: 函数名称。
            description: 函数描述。
            func: 函数对象（同步或异步）。
            parameters: 参数 Schema（JSON Schema 格式）。
        """
        self.function_registry.register(name, description, func, parameters)
