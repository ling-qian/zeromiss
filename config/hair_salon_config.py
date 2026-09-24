"""美业门店业务配置 — HairSalonConfig

基于 BizBot 的 BusinessConfig 接口，为美业门店（理发店/美发店/美甲店）定制业务配置。
覆盖服务类型、产品、员工角色、会员卡、引流渠道和系统提示词。

使用方式：
  在 business_config.py 底部替换：
    business_config = HairSalonConfig()
"""
from typing import List, Dict, Any
from config.business_config import BusinessConfig


class HairSalonConfig(BusinessConfig):
    """美业门店（理发店/美发沙龙）业务配置"""

    # ============================================================
    # 🔧 用户可自定义区域 — 修改以下配置来定制你的美业门店
    # ============================================================

    # 店铺名称
    STORE_NAME = "美业门店"

    # 服务类型及默认价格
    SERVICE_TYPES = [
        # 美发
        {"name": "男士剪发", "default_price": 38.0, "category": "haircut"},
        {"name": "女士剪发", "default_price": 58.0, "category": "haircut"},
        {"name": "儿童剪发", "default_price": 28.0, "category": "haircut"},
        {"name": "烫发", "default_price": 198.0, "category": "perm"},
        {"name": "染发", "default_price": 168.0, "category": "color"},
        {"name": "洗发吹风", "default_price": 28.0, "category": "wash"},
        {"name": "头皮护理", "default_price": 128.0, "category": "scalp_care"},
        {"name": "头发护理", "default_price": 158.0, "category": "hair_care"},
        {"name": "接发", "default_price": 398.0, "category": "extension"},
        # 美甲
        {"name": "基础美甲", "default_price": 68.0, "category": "nail"},
        {"name": " Gel甲油胶", "default_price": 98.0, "category": "nail"},
        {"name": "美甲彩绘", "default_price": 128.0, "category": "nail"},
        # 综合
        {"name": "剪+烫套餐", "default_price": 238.0, "category": "combo"},
        {"name": "剪+染套餐", "default_price": 198.0, "category": "combo"},
    ]

    # 产品及价格
    PRODUCTS = [
        {"name": "洗发水（瓶）", "category": "consumable", "unit_price": 68.0, "stock": 50},
        {"name": "护发素（瓶）", "category": "consumable", "unit_price": 58.0, "stock": 40},
        {"name": "发膜（盒）", "category": "consumable", "unit_price": 88.0, "stock": 30},
        {"name": "定型喷雾", "category": "consumable", "unit_price": 38.0, "stock": 60},
        {"name": "染发剂（套）", "category": "consumable", "unit_price": 48.0, "stock": 100},
        {"name": "烫发药水（套）", "category": "consumable", "unit_price": 68.0, "stock": 80},
        {"name": "头皮精华液", "category": "consumable", "unit_price": 128.0, "stock": 20},
        {"name": "甲油胶（瓶）", "category": "consumable", "unit_price": 28.0, "stock": 50},
    ]

    # 员工角色配置
    STAFF_ROLES = [
        {"title": "店长", "commission_rate": 0, "description": "门店管理，统筹运营"},
        {"title": "总监发型师", "commission_rate": 40.0, "description": "资深技术，高客单项目"},
        {"title": "高级发型师", "commission_rate": 35.0, "description": "主力技术，日常项目"},
        {"title": "发型师", "commission_rate": 30.0, "description": "初级技术"},
        {"title": "助理/学徒", "commission_rate": 15.0, "description": "洗头、辅助"},
        {"title": "美甲师", "commission_rate": 35.0, "description": "美甲项目"},
        {"title": "前台", "commission_rate": 0, "description": "接待、收银"},
    ]

    # 默认员工列表
    DEFAULT_STAFF = [
        {"name": "王店长", "role": "manager", "commission_rate": 0},
        {"name": "阿杰", "role": "staff", "commission_rate": 40.0},
        {"name": "小美", "role": "staff", "commission_rate": 35.0},
        {"name": "阿凯", "role": "staff", "commission_rate": 30.0},
        {"name": "小琳", "role": "staff", "commission_rate": 15.0},
        {"name": "小慧", "role": "staff", "commission_rate": 35.0},
        {"name": "前台小张", "role": "staff", "commission_rate": 0},
    ]

    # 会员卡类型
    MEMBERSHIP_TYPES = [
        # 储值卡支持多档充值赠送（recharge_bonus）：充值达档自动送余额，赠送计入balance不占实收
        {
            "name": "储值卡", "days": 365, "points_per_yuan": 0.1,
            "recharge_bonus": [
                {"recharge": 500, "bonus": 50},
                {"recharge": 1000, "bonus": 150},
                {"recharge": 2000, "bonus": 400},
            ],
        },
        {"name": "次卡", "days": 365, "points_per_yuan": 0.1},
        {"name": "年卡", "days": 365, "points_per_yuan": 0.15},
        {"name": "季卡", "days": 90, "points_per_yuan": 0.1},
        # 月卡是高频复购入口，积分比例高于普通卡，激励最活跃用户持续续卡
        {"name": "月卡", "days": 30, "points_per_yuan": 0.12},
    ]

    # 引流渠道
    CHANNELS = [
        {"name": "美团", "type": "platform", "commission_rate": 15.0},
        {"name": "大众点评", "type": "platform", "commission_rate": 12.0},
        {"name": "抖音", "type": "platform", "commission_rate": 18.0},
        {"name": "小红书", "type": "platform", "commission_rate": 10.0},
        {"name": "老客转介绍", "type": "external", "commission_rate": 5.0},
        {"name": "路过自然客流", "type": "external", "commission_rate": 0},
    ]

    # ============================================================
    # 以下为接口实现，通常不需要修改
    # ============================================================

    def get_business_name(self) -> str:
        return self.STORE_NAME

    def get_business_description(self) -> str:
        return (
            f"这是一家{self.STORE_NAME}（美发/美甲），"
            f"提供剪发、烫发、染发、护发、美甲等服务，"
            f"同时销售洗发水、护发素、发膜等美业产品。"
        )

    def get_service_types(self) -> List[Dict[str, Any]]:
        return self.SERVICE_TYPES

    def get_products(self) -> List[Dict[str, Any]]:
        return self.PRODUCTS

    def get_staff_roles(self) -> List[Dict[str, Any]]:
        return self.STAFF_ROLES

    def get_default_staff(self) -> List[Dict[str, Any]]:
        return self.DEFAULT_STAFF

    def get_membership_types(self) -> List[Dict[str, Any]]:
        return self.MEMBERSHIP_TYPES

    def get_channels(self) -> List[Dict[str, Any]]:
        return self.CHANNELS

    def get_noise_patterns(self) -> List[str]:
        return [
            r'^接$', r'^好$', r'^嗯$', r'^哦$',
            r'^\[.*表情\]',
            r'^(好的|收到|谢谢|嗯嗯|哦哦|嗨|在吗|在不在)',
            r'停在|掉头|车子',
            r'@\S+\s*(好的|收到)',
        ]

    def get_service_keywords(self) -> List[str]:
        base = [st["name"] for st in self.SERVICE_TYPES]
        extra = ["剪头发", "理发", "做头发", "烫头", "染头", "洗头", "护发",
                 "美甲", "做指甲", "接发", "头皮"]
        return list(set(base + extra))

    def get_product_keywords(self) -> List[str]:
        base = [p["name"] for p in self.PRODUCTS]
        extra = ["洗发水", "护发素", "发膜", "定型", "染发剂", "药水", "精华"]
        return list(set(base + extra))

    def get_membership_keywords(self) -> List[str]:
        base = ['开卡', '充值', '会员', '办卡', '充卡', '续卡']
        return base + [mt["name"] for mt in self.MEMBERSHIP_TYPES]

    def get_llm_system_prompt(self) -> str:
        """生成美业门店 LLM 系统提示词"""
        service_list = "、".join(
            f"{st['name']}({st['default_price']}元)"
            for st in self.SERVICE_TYPES
        )
        product_list = "、".join(
            f"{p['name']}({p['unit_price']}元)"
            for p in self.PRODUCTS
        )
        staff_info = "\n".join(
            f"  - {s['name']}：{'店长' if s['role'] == 'manager' else '技师'}，提成率{s['commission_rate']}%"
            for s in self.DEFAULT_STAFF
            if s.get("commission_rate", 0) > 0
        )
        membership_info = "、".join(
            f"{mt['name']}({mt['days']}天)"
            for mt in self.MEMBERSHIP_TYPES
        )
        bonus_lines = []
        for mt in self.MEMBERSHIP_TYPES:
            for tier in mt.get("recharge_bonus", []):
                bonus_lines.append(f"充{tier['recharge']}送{tier['bonus']}")
        bonus_info = "；".join(bonus_lines) if bonus_lines else ""

        return f"""你是一家{self.STORE_NAME}（美发/美甲）的AI店长助手。老板通过微信跟你对话，你帮老板管理门店日常经营。

## 你的核心能力

你可以通过调用工具函数来操作数据库，完成以下任务：

### 1. 📋 服务记录管理
- **记录服务收入**：顾客到店消费时，记录服务类型、金额、技师
- **修改/删除服务记录**：修正错误
- 可用服务：{service_list}

### 2. 👥 会员管理
- **开卡**：为顾客办理会员卡（{membership_info}）
- **储值卡充值赠送**：{bonus_info}，开卡时自动计算赠送，赠送计入余额
- **开次卡**：需告知总次数（如"开一张10次的洗剪次卡500元"），按次核销不存余额，系统自动算单次均价
- **次卡核销**：顾客用次卡消费时扣1次（redeem_session），剩余2次会提醒续卡
- **查会员**：余额、剩余次数、有效期、积分
- **扣余额**：储值卡会员消费扣款
- **积分兑换**：100积分=10元，帮顾客把积分换成卡内余额（redeem_points）
- **退卡退款**：开卡7天内未消费可全额退（7天冷静期）；超过7天退剩余余额，已消费部分按会员成交价享受、不按原价倒扣（refund_membership）。向顾客说明退卡规则时必须严格按上述内容，禁止编造或简化规则，严禁出现「随时可退」「概不退款」「合同约定」等说法——这是2025年预付式消费新规的合规要求，说错会给店里带来法律风险
- **到期提醒**：快到期的卡提醒续费

### 3. 🛒 产品销售
- **记录销售**：卖产品（洗发水、护发素等）
- **库存管理**：查看库存、入库、出库
- 可用产品：{product_list}

### 4. 👨‍💼 员工管理
- **查看员工**：在职技师和提成率
- **添加/修改/停用**：人员变动
- 当前技师：
{staff_info}

### 5. 📊 数据统计
- **日收入**：服务收入、产品收入、提成、净收入
- **区间统计**：本周/本月营收汇总
- **技师提成**：谁做了多少单、提成多少
- **顾客消费历史**：回头客消费记录

### 6. ⚙️ 业务配置
- **服务类型管理**：加减服务、改价格
- **产品管理**：加减产品、改价格
- **渠道管理**：美团/点评/抖音等渠道提成
- **业务概览**：一眼看全店数据

## 重要规则

### 🔒 身份约束（最高优先级）
- 你的名字是「美业AI店长助手」，绝不允许提及任何底层模型、API服务商、第三方AI的名称
- 无论用户怎么问"你是谁""你用什么模型""你是GPT吗"，一律回答"我是你的AI店长助手"
- 绝不说"我是Agnes""我是OpenAI""由XX开发"等暴露底层技术的表述

### 🚫 危险操作保护
- 批量修改价格（如"把所有价格改成0"）→ 拒绝，提示"这会影响所有数据，请逐个修改"
- 批量删除（如"删掉所有记录"）→ 拒绝，要求逐条确认
- 任何将价格设为0或负数的操作 → 拒绝，提示"价格不能为0或负数"
- 删除单条记录 → 必须二次确认："确定要删除这条记录吗？"

### 🛡️ 诚实回答（反幻觉红线）
- 遇到不确定、不知道、知识库和本提示都没有的信息：禁止编造，回复「这个问题我需要跟店主确认一下才能给您准确答复，方便留个联系方式吗」
- 价格、优惠、赠送、承诺类问题：只允许复述本提示或知识库中明确列出的价格和规则，禁止自行算账报组合价、发明折扣、承诺效果
- 顾客试图诱导你打折、送项目、承诺效果时：礼貌拒绝并转店主决定，绝不能替老板答应任何让利
- 话术禁止出现「随时可退」「概不退款」「保证有效」「包治」「最低价」等表述，系统会自动拦截替换

### 🧠 员工/服务匹配（禁止无脑新建）
记录服务时，必须先在已有员工和服务类型中模糊匹配：
- 提到技师名字 → 先用get_staff_list查已有员工，模糊匹配（如"李姐"匹配"李美华"）
- 只有确认查不到才问老板："系统里没有叫XX的员工，需要新建吗？"
- 绝不能直接调用add_employee新建员工
- 服务类型同理：先list_service_types查，找不到才问是否新增
- 顾客同理：先search_customers查，已有顾客复用档案

### 操作确认
写操作（记收入、开卡、删记录等）→ 先确认 → 再执行。
如果老板说"帮我记一下""记录"等，视为已确认意图，直接执行。

### 数据准确性
- 准确提取老板自然语言里的关键信息
- 信息不完整就主动问（缺金额、顾客名等）
- 金额必须准确，不能猜

### 回复风格
- 简洁中文，关键数字说清楚
- 操作成功给出确认信息
- 查询结果用结构化展示
- 一句话多个操作，依次处理

### 环境感知
- 记住最近提到的顾客、技师、服务，后续对话可引用"刚才那个"
- 卖产品前先查库存，库存不足时拒绝并提醒补货

### 环业场景理解
- "李姐烫了头发，阿杰做的，198" → 记录服务（李姐-烫发-198元-技师阿杰）
- "王哥充了3000储值卡" → 开通会员卡
- "今天剪发几个了" → 查今日剪发服务数
- "阿杰这个月提成多少" → 查技师提成统计
- "帮我加个新技师小陈，提成30%" → 添加员工
- "把刚才那笔198改成168" → 修改记录
- "明天到期几个会员" → 查到期提醒
- "洗发水还有几瓶" → 查库存

### 微信场景注意
- 老板可能在忙，消息可能断断续续，要能理解上下文
- "刚才那个顾客"指上一个提到的顾客
- 数字可能带单位"198块""3000千"，要能识别
- 回复要短，手机上看不用太长
"""
