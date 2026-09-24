"""回复审查器 ReplyGuard

LLM/知识库回复发给用户前的最后一道确定性闸门。

设计原则：
- 不依赖大模型：纯正则 + 集合比对，微秒级，行为可测试
- 两类检查：
  1. 违规话术黑名单 —— 对所有出口生效（LLM 生成 + 知识库直出）。
     知识库文案错误是真实发生过的事故源（见知识库 #17 教训），
     所以 KB 直出同样要过黑名单。
  2. 价格白名单 —— 仅对「无函数调用支撑的 LLM 回复」生效。
     有函数调用的数字来自数据库（可信）；知识库价格是店家授权事实；
     只有 LLM 裸生成的报价才可能凭空编造。
- 拦截动作：整条替换为按类别的安全合规话术，并记录日志供审计。
"""

import logging
import re
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# 违规话术黑名单：(违规词, 类别)。新增违规模式时在此追加，配合审计流程持续加固。
VIOLATION_RULES = [
    # 退款承诺类（预付式消费新规红线）
    ("随时可退", "refund"),
    ("随时退", "refund"),
    ("想退就退", "refund"),
    ("概不退款", "refund"),
    ("无效退款", "refund"),
    ("不满意全额退", "refund"),
    ("不满意可全额退", "refund"),
    # 效果承诺类（美业广告法红线）
    ("保证有效", "effect"),
    ("保证效果", "effect"),
    ("保证不掉色", "effect"),
    ("保证不脱色", "effect"),
    ("包治", "effect"),
    ("根治", "effect"),
    ("百分百有效", "effect"),
    ("绝对有效", "effect"),
    # 超范围承诺类
    ("终身有效", "promise"),
    ("永久有效", "promise"),
    ("永久免费", "promise"),
    ("假一赔", "promise"),
    # 价格违规类（广告法禁用）
    ("全网最低", "price"),
    ("全市最低", "price"),
    ("全城最低", "price"),
    ("全镇最低", "price"),
]

# 拦截后的替换话术（全部为合规表述）
SAFE_REPLIES = {
    "refund": (
        "退卡退款有明确规则的：办卡7天内未消费可以全额退，超过7天退剩余余额，"
        "次卡按剩余次数折算。具体金额需要店主跟您确认一下，我先把规则告诉您～"
    ),
    "effect": (
        "每个人情况不一样，我不能随便跟您打包票。到店后让技师根据您的实际发质/情况"
        "给专业建议，会更靠谱哦～"
    ),
    "price": (
        "具体价格以店内公示为准，不同项目和技师会有些差异。您方便的话留个联系方式，"
        "店主给您报个准确的价～"
    ),
    "promise": (
        "这个得店主说了算，我先帮您记下来，让店主尽快回复您～"
    ),
    "default": (
        "这个问题我需要跟店主确认一下才能给您准确答复，稍等哈～"
    ),
}

# 数字提取：¥88 / 88元 两种写法
_MONEY_PATTERNS = [
    re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(\d+(?:\.\d+)?)\s*元"),
]


class ReplyGuard:
    """对即将发出的回复做确定性安全审查。"""

    def __init__(self, config: Any = None):
        self.price_whitelist: Set[float] = set()
        self._combo_prices: Set[float] = set()
        try:
            self._load_prices(config)
        except Exception as e:  # 防御：价格白名单构建失败时降级为基础集合
            logger.error(f"ReplyGuard 价格白名单构建失败，使用基础集合: {e}")
            self._load_base_prices()

    # ---------- 价格白名单 ----------

    def _load_base_prices(self) -> None:
        self.price_whitelist = {0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0,
                                150.0, 200.0, 300.0, 400.0, 500.0, 1000.0,
                                2000.0, 3000.0, 5000.0}

    def _load_prices(self, config: Any) -> None:
        self._load_base_prices()
        if config is None:
            # 延迟导入默认业务配置（防御循环导入与缺失）
            try:
                import config.business_config as bc
                config = getattr(bc, "business_config", None)
            except Exception:
                config = None
        if config is None:
            return

        # 从配置提取真实价格
        # service_prices：服务/产品报价（可组合 = 顾客同时消费两项）
        # bonus_prices：充值赠送档位（单独放行，不参与报价组合——
        #   否则 38+150=188 这类数字会让编造报价蒙混过关）
        service_prices: Set[float] = set()
        bonus_prices: Set[float] = set()
        for st in getattr(config, "SERVICE_TYPES", []) or []:
            if isinstance(st, dict) and st.get("default_price") is not None:
                service_prices.add(float(st["default_price"]))
        for p in getattr(config, "PRODUCTS", []) or []:
            if isinstance(p, dict) and p.get("unit_price") is not None:
                service_prices.add(float(p["unit_price"]))
        for mt in getattr(config, "MEMBERSHIP_TYPES", []) or []:
            if not isinstance(mt, dict):
                continue
            for key in ("price", "amount", "default_amount"):
                if mt.get(key) is not None:
                    service_prices.add(float(mt[key]))
            for tier in mt.get("recharge_bonus", []) or []:
                bonus_prices.add(float(tier["recharge"]))
                bonus_prices.add(float(tier["bonus"]))

        self.price_whitelist |= service_prices | bonus_prices
        # 组合价规则：
        # 1. 服务/产品两两之和 —— 覆盖「两项同时消费」类合法算术
        # 2. 充值档+对应赠送 —— 覆盖「充500实际得550余额」类合法算术
        # 基础通用整数与赠送档位不参与任意组合，防止白名单被放大。
        cfg = sorted(service_prices)
        for i, a in enumerate(cfg):
            for b in cfg[i:]:
                s = a + b
                if s <= 10000:
                    self._combo_prices.add(round(s, 2))
        for mt in getattr(config, "MEMBERSHIP_TYPES", []) or []:
            if not isinstance(mt, dict):
                continue
            for tier in mt.get("recharge_bonus", []) or []:
                self._combo_prices.add(
                    round(float(tier["recharge"]) + float(tier["bonus"]), 2)
                )

    def _extract_amounts(self, text: str):
        found = []
        for pat in _MONEY_PATTERNS:
            for m in pat.finditer(text):
                try:
                    found.append(round(float(m.group(1)), 2))
                except ValueError:
                    continue
        return found

    # ---------- 主检查 ----------

    def check(
        self,
        reply_text: str,
        source: str = "llm",
        has_function_calls: bool = False,
    ) -> Dict[str, Any]:
        """审查回复。

        Returns:
            {
                "passed": bool,        # 是否通过（未替换）
                "replaced": bool,      # 是否被拦截替换
                "category": str|None,  # 违规类别
                "rule": str|None,      # 命中的具体规则/违规金额
                "content": str,        # 最终应发送的内容
            }
        """
        result = {
            "passed": True, "replaced": False,
            "category": None, "rule": None, "content": reply_text,
        }
        if not reply_text or not reply_text.strip():
            return result

        # 1) 黑名单：所有出口必查（含空白变体——「随 时 可 退」同样拦截）
        compact = re.sub(r"\s+", "", reply_text)
        for word, category in VIOLATION_RULES:
            if word in reply_text or word in compact:
                safe = SAFE_REPLIES.get(category, SAFE_REPLIES["default"])
                logger.warning(
                    f"[ReplyGuard] 拦截违规话术 source={source} "
                    f"rule={word!r} category={category} "
                    f"original={reply_text[:60]!r}"
                )
                result.update(passed=False, replaced=True,
                              category=category, rule=word, content=safe)
                return result

        # 2) 价格白名单：仅审查无函数调用支撑的 LLM 裸回复
        #    （KB 价格是店家授权事实；函数数字来自数据库，均跳过）
        if source == "llm" and not has_function_calls:
            allowed = self.price_whitelist | self._combo_prices
            for amount in self._extract_amounts(reply_text):
                if amount not in allowed:
                    safe = SAFE_REPLIES["price"]
                    logger.warning(
                        f"[ReplyGuard] 拦截可疑价格 {amount} "
                        f"original={reply_text[:60]!r}"
                    )
                    result.update(passed=False, replaced=True,
                                  category="price", rule=f"amount:{amount}",
                                  content=safe)
                    return result

        return result


_guard: Optional[ReplyGuard] = None


def get_reply_guard() -> ReplyGuard:
    """模块级单例，避免重复构建价格白名单。"""
    global _guard
    if _guard is None:
        _guard = ReplyGuard()
    return _guard
