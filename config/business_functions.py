"""业务函数模块 —— Agent 可调用的数据库操作工具集。

提供灵活的数据库增删改查函数，供 Agent 根据用户自然语言指令动态调用。
所有写操作（增/删/改）返回操作预览信息，需用户确认后才真正执行。

设计原则：
- 查询操作直接执行并返回结果
- 写操作分两步：先预览（prepare），再确认执行（confirm）
- 函数覆盖所有核心业务实体的增删改查
- 使用中文描述，方便 LLM 理解
"""
import json
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List

from loguru import logger

# 全局数据库实例引用（由 app.py 设置）
_db = None
# 待确认的操作缓存 {session_id: {operation_id: operation_data}}
_pending_operations: Dict[str, Dict[str, Any]] = {}
# 操作计数器
_op_counter = 0


def set_db(db_manager):
    """设置数据库管理器实例（由 app.py 调用）。"""
    global _db
    _db = db_manager


def _get_db():
    """获取数据库实例。"""
    assert _db is not None, "数据库未初始化，请先调用 set_db()"
    return _db


def _next_op_id() -> str:
    """生成下一个操作 ID。"""
    global _op_counter
    _op_counter += 1
    return f"op_{_op_counter}"


def _parse_date(date_str: Optional[str] = None) -> date:
    """解析日期字符串，默认今天。格式非法时抛 ValueError（由调用方转成 error 返回给 AI 自我纠正）。"""
    if not date_str:
        return date.today()
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"日期格式不合法：「{date_str}」，请使用 YYYY-MM-DD 格式（如 2026-09-15）")


# ================================================================
# 服务记录相关
# ================================================================


def record_service(
    customer_name: str,
    service_type: str,
    amount: float,
    employee_name: Optional[str] = None,
    date_str: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    notes: Optional[str] = None,
) -> dict:
    """记录一笔服务收入。

    Args:
        customer_name: 顾客姓名（必填）
        service_type: 服务类型名称，如"推拿按摩"、"艾灸理疗"（必填）
        amount: 服务金额（必填）
        employee_name: 服务员工/技师名称（可选）
        date_str: 日期，格式YYYY-MM-DD，默认今天（可选）
        duration_minutes: 服务时长（分钟）（可选）
        notes: 备注信息（可选）

    Returns:
        操作结果，包含记录详情
    """
    db = _get_db()
    try:
        # 🚫 金额校验
        if not isinstance(amount, (int, float)) or amount <= 0:
            return {"success": False, "error": f"⚠️ 服务金额必须大于0，当前值: {amount}"}
        # 🚫 顾客名非空校验
        if not customer_name or not str(customer_name).strip():
            return {"success": False, "error": "⚠️ 顾客姓名不能为空，请提供顾客姓名"}
        customer_name = str(customer_name).strip()

        service_date = _parse_date(date_str)

        # 查找员工和提成
        commission = 0.0
        referral_channel_id = None
        if employee_name:
            with db.get_session() as session:
                from database.models import Employee
                emp = session.query(Employee).filter(
                    Employee.name == employee_name
                ).first()
                if emp and emp.commission_rate:
                    rate = float(emp.commission_rate)
                    commission = amount * (rate / 100.0)

            # 创建/获取渠道
            channel = db.channels.get_or_create(
                employee_name, "internal", None,
                float(emp.commission_rate) if emp and emp.commission_rate else 0
            )
            referral_channel_id = channel.id

        # 构建备注
        full_notes = ""
        if duration_minutes:
            full_notes += f"时长{duration_minutes}分钟"
        if notes:
            full_notes += f"；{notes}" if full_notes else notes

        msg_id = db.save_raw_message({
            "msg_id": f"agent_svc_{datetime.now().timestamp()}",
            "sender_nickname": "管理助手",
            "content": f"{customer_name} {service_type} {amount}元",
            "timestamp": datetime.now(),
        })

        record_id = db.save_service_record({
            "customer_name": customer_name,
            "service_or_product": service_type,
            "date": service_date,
            "amount": amount,
            "commission": commission,
            "referral_channel_id": referral_channel_id,
            "net_amount": amount - commission,
            "notes": full_notes or None,
            "confirmed": True,
        }, msg_id)

        return {
            "success": True,
            "message": f"✅ 已记录服务：{customer_name} - {service_type} {amount}元",
            "record_id": record_id,
            "customer": customer_name,
            "service": service_type,
            "amount": amount,
            "employee": employee_name or "未指定",
            "commission": commission,
            "net_income": amount - commission,
            "duration": f"{duration_minutes}分钟" if duration_minutes else "未记录",
            "date": str(service_date),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_service_record(record_id: int, reason: Optional[str] = None) -> dict:
    """删除一条服务记录。

    Args:
        record_id: 服务记录ID（必填）
        reason: 删除原因（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import ServiceRecord
        with db.get_session() as session:
            record = session.query(ServiceRecord).filter(
                ServiceRecord.id == record_id
            ).first()
            if not record:
                return {"success": False, "message": f"未找到ID为{record_id}的服务记录"}

            info = {
                "customer": record.customer.name if record.customer else "未知",
                "service": record.service_type.name if record.service_type else "未知",
                "amount": float(record.amount),
                "date": str(record.service_date),
            }

        # 保存修正记录
        db.messages.save_correction({
            "original_record_type": "service_records",
            "original_record_id": record_id,
            "correction_type": "delete",
            "old_value": info,
            "reason": reason or "用户通过助手删除",
        })

        # 执行删除
        from database.models import ServiceRecord
        result = db.service_records.delete_by_id(ServiceRecord, record_id)

        if result:
            return {
                "success": True,
                "message": f"✅ 已删除服务记录 #{record_id}：{info['customer']} - {info['service']} {info['amount']}元",
                "deleted_record": info,
            }
        return {"success": False, "message": "删除失败"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def update_service_record(
    record_id: int,
    amount: Optional[float] = None,
    service_type: Optional[str] = None,
    date_str: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """修改一条服务记录的信息。

    Args:
        record_id: 服务记录ID（必填）
        amount: 新金额（可选）
        service_type: 新服务类型（可选）
        date_str: 新日期，格式YYYY-MM-DD（可选）
        notes: 新备注（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import ServiceRecord
        update_kwargs = {}
        if amount is not None:
            update_kwargs["amount"] = amount
            update_kwargs["net_amount"] = amount  # 简化处理
        if date_str:
            update_kwargs["service_date"] = _parse_date(date_str)
        if notes is not None:
            update_kwargs["notes"] = notes

        if not update_kwargs:
            return {"success": False, "message": "未提供需要修改的字段"}

        result = db.service_records.update_by_id(
            ServiceRecord, record_id, **update_kwargs
        )
        if result:
            return {
                "success": True,
                "message": f"✅ 已更新服务记录 #{record_id}",
                "updated_fields": {k: str(v) for k, v in update_kwargs.items()},
            }
        return {"success": False, "message": f"未找到ID为{record_id}的服务记录"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 会员管理相关
# ================================================================


def open_membership(
    customer_name: str,
    card_type: str,
    amount: float,
    sessions: Optional[int] = None,
    date_str: Optional[str] = None,
) -> dict:
    """为顾客开通会员卡/疗程卡/储值卡/次卡。

    - 储值卡：自动匹配充值赠送档位（配置 recharge_bonus），赠送计入余额
    - 次卡：需传 sessions（总次数），按次核销，不存余额
    - 其他卡型：充值金额即余额

    Args:
        customer_name: 顾客姓名（必填）
        card_type: 卡类型，如"年卡"、"季卡"、"月卡"、"次卡"、"疗程卡"、"储值卡"（必填）
        amount: 充值/购卡金额（必填）
        sessions: 次卡总次数，仅次卡/疗程卡需要（可选）
        date_str: 开卡日期，格式YYYY-MM-DD，默认今天（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import Membership
        # 顾客名非空校验：空名/纯空格会创建无主卡，后续无法核销和召回
        if not customer_name or not str(customer_name).strip():
            return {"success": False, "error": "顾客姓名不能为空，请提供顾客姓名"}
        customer_name = str(customer_name).strip()
        opened_date = _parse_date(date_str)

        # 卡型有效期/积分比例统一由 config.MEMBERSHIP_TYPES 驱动，避免配置与执行两张皮
        from config.business_config import business_config
        card_cfg = {c["name"]: c for c in business_config.get_membership_types()}
        # 卡型白名单校验：不存在的卡型直接拒绝（防止AI识别错误开出奇怪卡型）
        valid_cards = set(card_cfg.keys()) | {"疗程卡"}
        if card_type not in valid_cards:
            names = "、".join(sorted(valid_cards))
            return {
                "success": False,
                "error": f"暂不支持「{card_type}」，当前支持的卡型：{names}。如需新卡型请先在业务配置中添加",
            }
        cfg = card_cfg.get(card_type, {})
        days = cfg.get("days", 180 if card_type == "疗程卡" else 365)
        points_per_yuan = cfg.get("points_per_yuan", 0.1)
        points = int(amount * points_per_yuan)

        # 次卡校验：必须指定次数，余额为0（次卡按次核销，不存钱）
        if not isinstance(amount, (int, float)) or amount <= 0:
            return {"success": False, "error": "开卡金额必须大于0"}
        is_session_card = card_type in ("次卡", "疗程卡")
        if is_session_card:
            if not isinstance(sessions, int) or sessions <= 0:
                return {
                    "success": False,
                    "error": "开次卡/疗程卡需要指定总次数（sessions），例如：给张姐开一张10次的洗剪次卡500元",
                }
            if amount <= 0:
                return {"success": False, "error": f"购卡金额必须大于0，收到：{amount}"}
            balance = 0.0
            per_use = round(amount / sessions, 2)
        else:
            # 储值卡充值赠送：匹配最高适用档位，赠送计入余额
            bonus = 0.0
            for tier in cfg.get("recharge_bonus", []):
                if amount >= tier["recharge"]:
                    bonus = float(tier["bonus"])
            balance = float(amount) + bonus

        msg_id = db.save_raw_message({
            "msg_id": f"agent_mem_{datetime.now().timestamp()}",
            "sender_nickname": "管理助手",
            "content": f"{customer_name}开{card_type}{amount}元",
            "timestamp": datetime.now(),
        })

        membership_id = db.save_membership({
            "customer_name": customer_name,
            "card_type": card_type,
            "date": opened_date,
            "amount": amount,
            "balance": balance,
            "remaining_sessions": sessions if is_session_card else None,
            "expires_at": str(opened_date + timedelta(days=days)),
            # 次卡记录总次数，退卡时按剩余比例折算退款
            "extra_data": {"total_sessions": sessions} if is_session_card else {},
        }, msg_id)

        # 设置积分（有效期已在save时写入）
        with db.get_session() as session:
            membership = session.query(Membership).filter(
                Membership.id == membership_id
            ).first()
            if membership:
                membership.points = points
                session.commit()

        result = {
            "success": True,
            "membership_id": membership_id,
            "customer": customer_name,
            "card_type": card_type,
            "amount": amount,
            "valid_days": days,
            "expires_at": str(opened_date + timedelta(days=days)),
            "points": points,
        }
        if is_session_card:
            result["message"] = (
                f"✅ 已为{customer_name}开通{card_type}：{sessions}次，购卡{amount}元"
                f"（单次合{per_use}元），有效期{days}天"
            )
            result["total_sessions"] = sessions
            result["per_use_price"] = per_use
        else:
            result["message"] = f"✅ 已为{customer_name}开通{card_type}，充值{amount}元"
            result["balance"] = balance
            if bonus > 0:
                result["message"] += f"，赠送{bonus:g}元，到账余额{balance:g}元"
                result["bonus"] = bonus
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


def query_member_info(customer_name: str) -> dict:
    """查询顾客/会员信息，包括会员卡、余额、有效期等。

    Args:
        customer_name: 顾客姓名（必填）

    Returns:
        顾客信息
    """
    db = _get_db()
    try:
        from database.models import Customer
        with db.get_session() as session:
            customer = session.query(Customer).filter(
                Customer.name == customer_name
            ).first()

            if not customer:
                return {"success": False, "message": f"未找到顾客：{customer_name}"}

            memberships = []
            for m in customer.memberships:
                memberships.append({
                    "id": m.id,
                    "card_type": m.card_type,
                    "balance": float(m.balance),
                    "total_amount": float(m.total_amount),
                    "opened_at": str(m.opened_at),
                    "expires_at": str(m.expires_at) if m.expires_at else None,
                    "points": m.points,
                    "is_active": m.is_active,
                    "remaining_sessions": m.remaining_sessions,
                })

            service_count = len(customer.service_records)
            product_count = len(customer.product_sales)

        return {
            "success": True,
            "customer": customer_name,
            "phone": customer.phone,
            "notes": customer.notes,
            "memberships": memberships,
            "statistics": {
                "total_cards": len(memberships),
                "service_count": service_count,
                "product_count": product_count,
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def query_expiring_members(days: int = 7) -> dict:
    """查询即将到期的会员卡。

    Args:
        days: 查询未来多少天内到期的会员卡，默认7天

    Returns:
        即将到期的会员列表
    """
    db = _get_db()
    try:
        from database.models import Membership
        today = date.today()
        deadline = today + timedelta(days=days)

        with db.get_session() as session:
            expiring = session.query(Membership).filter(
                Membership.is_active == True,
                Membership.expires_at != None,
                Membership.expires_at <= deadline,
                Membership.expires_at >= today,
            ).all()

            results = []
            for m in expiring:
                results.append({
                    "customer": m.customer.name if m.customer else "未知",
                    "card_type": m.card_type,
                    "expires_at": str(m.expires_at),
                    "balance": float(m.balance),
                    "days_left": (m.expires_at - today).days,
                })

        return {
            "success": True,
            "message": f"未来{days}天内有{len(results)}张会员卡即将到期",
            "expiring_count": len(results),
            "members": results,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def deduct_membership_balance(
    membership_id: int,
    amount: float,
) -> dict:
    """扣减会员卡余额。

    Args:
        membership_id: 会员卡ID（必填）
        amount: 扣减金额（必填）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        result = db.memberships.deduct_balance(membership_id, amount)
        if result:
            return {
                "success": True,
                "message": f"✅ 已扣减会员卡 #{membership_id} 余额 {amount}元，剩余 {float(result.balance)}元",
                "remaining_balance": float(result.balance),
            }
        return {"success": False, "message": "扣减失败，可能余额不足"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def redeem_session(membership_id: int, service_name: Optional[str] = None) -> dict:
    """次卡/疗程卡核销：扣减1次剩余次数。

    Args:
        membership_id: 会员卡ID（必填）
        service_name: 本次使用的服务名称（可选，仅用于记录备注）

    Returns:
        操作结果，含剩余次数
    """
    db = _get_db()
    try:
        from database.models import Membership
        with db.get_session() as session:
            m = session.query(Membership).filter(Membership.id == membership_id).first()
            if not m:
                return {"success": False, "error": f"找不到会员卡 #{membership_id}"}
            if m.remaining_sessions is None:
                return {
                    "success": False,
                    "error": f"会员卡 #{membership_id}（{m.card_type}）不是次卡，不能用扣次，请用扣减余额",
                }
            if m.remaining_sessions <= 0:
                return {
                    "success": False,
                    "error": f"会员卡 #{membership_id} 次数已用完，请提示顾客续卡或升级",
                }
            if not m.is_active:
                return {"success": False, "error": f"会员卡 #{membership_id} 已过期或停用"}
            # 自然过期校验：is_active 为真但已过有效期，同样不能核销
            if m.expires_at and m.expires_at < date.today():
                return {"success": False, "error": f"会员卡 #{membership_id} 已于 {m.expires_at} 到期，无法核销，请提示顾客续卡"}
            m.remaining_sessions -= 1
            remaining = m.remaining_sessions
            session.commit()

        svc = f"（{service_name}）" if service_name else ""
        msg = f"✅ 已核销会员卡 #{membership_id}{svc} 1次，剩余 {remaining} 次"
        if remaining <= 2:
            msg += f"，⚠️ 次数不多，记得提醒顾客续卡"
        return {
            "success": True,
            "message": msg,
            "remaining_sessions": remaining,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def refund_membership(membership_id: int, reason: Optional[str] = None) -> dict:
    """会员卡退卡退款（符合预付式消费合规要求）。

    规则（依据最高法预付式消费司法解释）：
    - 开卡7天内且未产生任何消费（余额未动）：全额退款（7天冷静期）
    - 超过7天：退剩余余额，已消费部分按会员成交价享受，不按原价倒扣

    Args:
        membership_id: 会员卡ID（必填）
        reason: 退卡原因（可选，仅记录）

    Returns:
        操作结果，含退款金额与规则说明
    """
    db = _get_db()
    try:
        from database.models import Membership, ServiceRecord
        with db.get_session() as session:
            m = session.query(Membership).filter(Membership.id == membership_id).first()
            if not m:
                return {"success": False, "error": f"找不到会员卡 #{membership_id}"}
            if not m.is_active:
                return {"success": False, "error": f"会员卡 #{membership_id} 已失效，无需退卡"}
            if m.remaining_sessions is not None:
                # 次卡/疗程卡退款：按剩余次数占开卡总次数比例折算
                total_sessions = (m.extra_data or {}).get("total_sessions")
                if isinstance(total_sessions, (int, float)) and total_sessions > 0:
                    remaining = max(int(m.remaining_sessions), 0)
                    opened = m.opened_at
                    within_cooldown = (date.today() - opened).days <= 7 and remaining == int(total_sessions)
                    if within_cooldown:
                        refund_amount = float(m.total_amount)
                        rule = f"7天冷静期内未核销，全额退款"
                    else:
                        refund_amount = round(float(m.total_amount) * remaining / total_sessions, 2)
                        if remaining == 0:
                            rule = "次数已用完，无可退金额，仅办理退卡"
                        else:
                            rule = f"按剩余 {remaining}/{int(total_sessions)} 次折算退款（已核销部分不退）"
                    # 统一状态清理：退卡后立即失效，防止继续核销
                    m.is_active = False
                    m.balance = 0
                    m.remaining_sessions = 0
                    m.points = 0
                    session.commit()
                    msg = f"✅ 已为会员卡 #{membership_id} 办理退卡，退款 {refund_amount:g} 元。规则：{rule}"
                    if reason:
                        msg += f"。原因：{reason}"
                    return {"success": True, "message": msg, "refund_amount": refund_amount}
                # 老数据没有总次数记录：先失效防继续核销，再转人工
                m.is_active = False
                session.commit()
                return {
                    "success": False,
                    "error": f"次卡 #{membership_id} 缺少开卡总次数记录，已先停用该卡防止误核销。剩余{m.remaining_sessions}次，请人工核算退款后到会员管理页确认",
                }

            balance = float(m.balance)
            total = float(m.total_amount)
            opened = m.opened_at
            # 是否有真实消费记录（服务记录关联该卡）
            has_consumption = session.query(ServiceRecord).filter(
                ServiceRecord.membership_id == membership_id
            ).count() > 0
            within_cooldown = (date.today() - opened).days <= 7 and not has_consumption

            if within_cooldown:
                refund_amount = total
                rule = "7天冷静期内且未消费，全额退款"
            else:
                refund_amount = balance
                if has_consumption:
                    rule = "退剩余余额（已消费部分按会员成交价享受，不按原价倒扣）"
                else:
                    rule = "已超过7天冷静期，退剩余余额"

            m.is_active = False
            m.balance = 0
            m.remaining_sessions = 0
            m.points = 0
            session.commit()

        msg = f"✅ 已为会员卡 #{membership_id} 办理退卡，退款 {refund_amount:g} 元。规则：{rule}"
        if reason:
            msg += f"。原因：{reason}"
        return {
            "success": True,
            "message": msg,
            "refund_amount": refund_amount,
            "rule": rule,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def redeem_points(customer_name: str, points: int, item: Optional[str] = None) -> dict:
    """积分兑换：将会员积分按100积分=10元折算计入余额。

    Args:
        customer_name: 顾客姓名（必填）
        points: 兑换的积分数（必填，须为100的整数倍，100积分=10元）
        item: 兑换说明，如"兑换洗发水"、"积分抵现"（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import Customer, Membership
        if not isinstance(points, int) or points <= 0 or points % 100 != 0:
            return {
                "success": False,
                "error": "兑换积分须为正整数且为100的整数倍（100积分=10元）",
            }

        with db.get_session() as session:
            customer = session.query(Customer).filter(
                Customer.name == customer_name
            ).first()
            if not customer:
                return {"success": False, "error": f"找不到顾客「{customer_name}」"}
            m = (
                session.query(Membership)
                .filter(Membership.customer_id == customer.id, Membership.is_active == True)  # noqa: E712
                .order_by(Membership.id.desc())
                .first()
            )
            if not m:
                return {"success": False, "error": f"顾客「{customer_name}」没有有效会员卡"}
            if m.points < points:
                return {
                    "success": False,
                    "error": f"积分不足：当前{m.points}分，需{points}分",
                }
            m.points -= points
            value = points / 10
            m.balance = float(m.balance) + value
            remaining_points = m.points
            new_balance = float(m.balance)
            session.commit()

        note = f"，兑换「{item}」" if item else ""
        return {
            "success": True,
            "message": (
                f"✅ {customer_name} 用 {points} 积分兑换了 {value:g} 元余额{note}，"
                f"剩余积分{remaining_points}，卡内余额{new_balance:g}元"
            ),
            "redeemed_value": value,
            "remaining_points": remaining_points,
            "balance": new_balance,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 产品销售相关
# ================================================================


def record_product_sale(
    product_name: str,
    amount: float,
    customer_name: Optional[str] = None,
    quantity: int = 1,
    date_str: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """记录产品/商品销售。

    Args:
        product_name: 产品名称（必填）
        amount: 总金额（必填）
        customer_name: 顾客姓名（可选）
        quantity: 数量，默认1
        date_str: 日期，格式YYYY-MM-DD，默认今天（可选）
        notes: 备注（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        # 🚫 基础校验：数量和金额必须为正
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            return {"success": False, "error": f"⚠️ 销售数量必须大于0，当前值: {quantity}"}
        if not isinstance(amount, (int, float)) or amount <= 0:
            return {"success": False, "error": f"⚠️ 销售金额必须大于0，当前值: {amount}"}
        # 🚫 顾客名非空校验（销售记录顾客可选，但填了就不能是空串）
        if customer_name is not None and not str(customer_name).strip():
            return {"success": False, "error": "⚠️ 顾客姓名不能为空白，请提供顾客姓名或不填"}

        sale_date = _parse_date(date_str)

        # 📦 库存校验：销售前检查库存是否充足 + 产品是否存在
        from database.models import Product
        product_match = None
        with db.get_session() as session:
            candidates = session.query(Product).all()
            for p in candidates:
                if p.name == product_name or product_name in p.name or p.name in product_name:
                    product_match = p
                    break

        # 产品必须存在
        if not product_match:
            return {
                "success": False,
                "error": f"⚠️ 产品「{product_name}」不存在，请先添加产品或检查名称是否正确。当前产品：{', '.join(p.name for p in candidates)}",
            }

        # 库存必须充足
        if product_match.stock_quantity is not None and product_match.stock_quantity < quantity:
            return {
                "success": False,
                "error": f"⚠️ 库存不足：{product_match.name}当前库存{product_match.stock_quantity}件，不够卖{quantity}件。请先入库补货。",
                "current_stock": product_match.stock_quantity,
                "requested_quantity": quantity,
            }

        msg_id = db.save_raw_message({
            "msg_id": f"agent_prod_{datetime.now().timestamp()}",
            "sender_nickname": "管理助手",
            "content": f"{customer_name or '顾客'}购买{product_name}{amount}元",
            "timestamp": datetime.now(),
        })

        sale_id = db.save_product_sale({
            "service_or_product": product_name,
            "date": sale_date,
            "amount": amount,
            "quantity": quantity,
            "unit_price": round(amount / quantity, 2),
            "customer_name": customer_name,
            "notes": notes,
            "confirmed": True,
        }, msg_id)

        # 自动扣减库存并提交
        remaining_stock = None
        if product_match and product_match.stock_quantity is not None:
            with db.get_session() as session:
                p = session.query(Product).get(product_match.id)
                if p:
                    p.stock_quantity = p.stock_quantity - quantity
                    remaining_stock = p.stock_quantity
                    session.commit()

        result = {
            "success": True,
            "message": f"✅ 已记录产品销售：{product_name} x{quantity} 共{amount}元",
            "sale_id": sale_id,
            "product": product_name,
            "quantity": quantity,
            "amount": amount,
            "customer": customer_name or "散客",
            "date": str(sale_date),
        }
        if remaining_stock is not None:
            result["remaining_stock"] = remaining_stock
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_product_sale(record_id: int, reason: Optional[str] = None) -> dict:
    """删除一条产品销售记录。

    Args:
        record_id: 销售记录ID（必填）
        reason: 删除原因（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import ProductSale
        result = db.product_sales.delete_by_id(ProductSale, record_id)
        if result:
            return {
                "success": True,
                "message": f"✅ 已删除产品销售记录 #{record_id}",
            }
        return {"success": False, "message": f"未找到ID为{record_id}的销售记录"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 员工管理相关
# ================================================================


def get_staff_list() -> dict:
    """获取员工/技师列表。

    Returns:
        所有在职员工信息
    """
    db = _get_db()
    try:
        staff = db.get_staff_list(active_only=True)
        return {
            "success": True,
            "message": f"共有{len(staff)}名在职员工",
            "staff_count": len(staff),
            "staff": staff,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def add_employee(
    name: str,
    role: str = "staff",
    commission_rate: float = 0,
) -> dict:
    """添加新员工/技师。

    Args:
        name: 员工姓名（必填）
        role: 角色，如"staff"（普通员工）、"manager"（管理员）（可选，默认staff）
        commission_rate: 提成率（百分比，如30表示30%）（可选，默认0）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import Employee
        with db.get_session() as session:
            existing = session.query(Employee).filter(
                Employee.name == name
            ).first()
            if existing:
                return {"success": False, "message": f"员工'{name}'已存在"}

        employee = db.staff.get_or_create(name)
        # 更新角色和提成率
        from database.models import Employee
        db.staff.update_by_id(
            Employee, employee.id,
            role=role,
            commission_rate=commission_rate,
        )

        return {
            "success": True,
            "message": f"✅ 已添加员工：{name}（{role}，提成率{commission_rate}%）",
            "employee_id": employee.id,
            "name": name,
            "role": role,
            "commission_rate": commission_rate,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def update_employee(
    name: str,
    role: Optional[str] = None,
    commission_rate: Optional[float] = None,
    is_active: Optional[bool] = None,
) -> dict:
    """修改员工信息。

    Args:
        name: 员工姓名（必填，用于查找员工）
        role: 新角色（可选）
        commission_rate: 新提成率（可选）
        is_active: 是否在职（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import Employee
        with db.get_session() as session:
            employee = session.query(Employee).filter(
                Employee.name == name
            ).first()
            if not employee:
                return {"success": False, "message": f"未找到员工：{name}"}
            emp_id = employee.id

        update_kwargs = {}
        if role is not None:
            update_kwargs["role"] = role
        if commission_rate is not None:
            update_kwargs["commission_rate"] = commission_rate
        if is_active is not None:
            update_kwargs["is_active"] = is_active

        if not update_kwargs:
            return {"success": False, "message": "未提供需要修改的字段"}

        db.staff.update_by_id(Employee, emp_id, **update_kwargs)
        return {
            "success": True,
            "message": f"✅ 已更新员工 {name} 的信息",
            "updated_fields": update_kwargs,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def remove_employee(name: str) -> dict:
    """停用员工（软删除，标记为不在职）。

    Args:
        name: 员工姓名（必填）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import Employee
        with db.get_session() as session:
            employee = session.query(Employee).filter(
                Employee.name == name
            ).first()
            if not employee:
                return {"success": False, "message": f"未找到员工：{name}"}
            emp_id = employee.id

        db.staff.deactivate(emp_id)
        return {
            "success": True,
            "message": f"✅ 已将员工 {name} 标记为离职",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 顾客管理相关
# ================================================================


def add_customer(
    name: str,
    phone: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """添加新顾客。

    Args:
        name: 顾客姓名（必填）
        phone: 联系电话（可选）
        notes: 备注信息（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        customer = db.customers.get_or_create(name)
        from database.models import Customer
        update_kwargs = {}
        if phone:
            update_kwargs["phone"] = phone
        if notes:
            update_kwargs["notes"] = notes
        if update_kwargs:
            db.customers.update_by_id(Customer, customer.id, **update_kwargs)

        return {
            "success": True,
            "message": f"✅ 已添加顾客：{name}",
            "customer_id": customer.id,
            "name": name,
            "phone": phone,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def update_customer(
    name: str,
    phone: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """修改顾客信息。

    Args:
        name: 顾客姓名（必填，用于查找顾客）
        phone: 新电话（可选）
        notes: 新备注（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import Customer
        with db.get_session() as session:
            customer = session.query(Customer).filter(
                Customer.name == name
            ).first()
            if not customer:
                return {"success": False, "message": f"未找到顾客：{name}"}
            cust_id = customer.id

        update_kwargs = {}
        if phone is not None:
            update_kwargs["phone"] = phone
        if notes is not None:
            update_kwargs["notes"] = notes

        if not update_kwargs:
            return {"success": False, "message": "未提供需要修改的字段"}

        db.customers.update_by_id(Customer, cust_id, **update_kwargs)
        return {
            "success": True,
            "message": f"✅ 已更新顾客 {name} 的信息",
            "updated_fields": update_kwargs,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def search_customers(keyword: str) -> dict:
    """搜索顾客（按姓名或电话模糊搜索）。

    Args:
        keyword: 搜索关键词（必填）

    Returns:
        匹配的顾客列表
    """
    db = _get_db()
    try:
        customers = db.customers.search(keyword)
        return {
            "success": True,
            "message": f"找到{len(customers)}名匹配的顾客",
            "count": len(customers),
            "customers": [
                {
                    "id": c.id,
                    "name": c.name,
                    "phone": c.phone,
                    "notes": c.notes,
                }
                for c in customers
            ],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 服务类型管理
# ================================================================


def list_service_types() -> dict:
    """列出所有服务类型及其默认价格。

    Returns:
        服务类型列表
    """
    db = _get_db()
    try:
        from database.models import ServiceType
        with db.get_session() as session:
            types = session.query(ServiceType).all()
            result = [
                {
                    "id": t.id,
                    "name": t.name,
                    "default_price": float(t.default_price) if t.default_price else None,
                    "category": t.category,
                }
                for t in types
            ]
        return {
            "success": True,
            "message": f"共有{len(result)}种服务类型",
            "service_types": result,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def add_service_type(
    name: str,
    default_price: Optional[float] = None,
    category: Optional[str] = None,
) -> dict:
    """添加新的服务类型。

    Args:
        name: 服务类型名称（必填）
        default_price: 默认价格（可选）
        category: 类别（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        st = db.service_types.get_or_create(name, default_price, category)
        return {
            "success": True,
            "message": f"✅ 已添加服务类型：{name}" + (f"（默认价格{default_price}元）" if default_price else ""),
            "service_type_id": st.id,
            "name": name,
            "default_price": default_price,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def update_service_type(
    name: str,
    new_price: Optional[float] = None,
    new_category: Optional[str] = None,
) -> dict:
    """修改服务类型信息（价格、类别等）。

    Args:
        name: 服务类型名称（必填，用于查找）
        new_price: 新默认价格（可选）
        new_category: 新类别（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import ServiceType
        with db.get_session() as session:
            st = session.query(ServiceType).filter(
                ServiceType.name == name
            ).first()
            if not st:
                return {"success": False, "message": f"未找到服务类型：{name}"}
            st_id = st.id

        update_kwargs = {}
        if new_price is not None:
            update_kwargs["default_price"] = new_price
        if new_category is not None:
            update_kwargs["category"] = new_category

        if not update_kwargs:
            return {"success": False, "message": "未提供需要修改的字段"}

        db.service_types.update_by_id(ServiceType, st_id, **update_kwargs)
        return {
            "success": True,
            "message": f"✅ 已更新服务类型 {name}",
            "updated_fields": update_kwargs,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 产品管理
# ================================================================


def list_products() -> dict:
    """列出所有产品/商品及其价格和库存。

    Returns:
        产品列表
    """
    db = _get_db()
    try:
        from database.models import Product
        with db.get_session() as session:
            products = session.query(Product).all()
            result = [
                {
                    "id": p.id,
                    "name": p.name,
                    "category": p.category,
                    "unit_price": float(p.unit_price) if p.unit_price else None,
                    "stock_quantity": p.stock_quantity,
                }
                for p in products
            ]
        return {
            "success": True,
            "message": f"共有{len(result)}种产品",
            "products": result,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def add_product(
    name: str,
    category: Optional[str] = None,
    unit_price: Optional[float] = None,
    stock_quantity: int = 0,
) -> dict:
    """添加新产品/商品。

    Args:
        name: 产品名称（必填）
        category: 类别，如"consumable"（消耗品）、"tool"（工具）（可选）
        unit_price: 单价（可选）
        stock_quantity: 初始库存数量（可选，默认0）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        product = db.products.get_or_create(name, category, unit_price)
        if stock_quantity > 0:
            from database.models import Product
            db.products.update_by_id(Product, product.id, stock_quantity=stock_quantity)

        return {
            "success": True,
            "message": f"✅ 已添加产品：{name}" + (f"（单价{unit_price}元）" if unit_price else ""),
            "product_id": product.id,
            "name": name,
            "unit_price": unit_price,
            "stock_quantity": stock_quantity,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def update_product_stock(
    product_name: str,
    quantity_change: int,
    reason: Optional[str] = None,
) -> dict:
    """更新产品库存（入库或出库）。

    Args:
        product_name: 产品名称（必填）
        quantity_change: 数量变动，正数表示入库，负数表示出库（必填）
        reason: 变动原因（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        from database.models import Product
        with db.get_session() as session:
            product = session.query(Product).filter(
                Product.name == product_name
            ).first()
            if not product:
                return {"success": False, "message": f"未找到产品：{product_name}"}
            pid = product.id

        result = db.products.update_stock(pid, quantity_change)
        if result:
            action = "入库" if quantity_change > 0 else "出库"
            return {
                "success": True,
                "message": f"✅ {product_name} {action} {abs(quantity_change)}件，当前库存 {result.stock_quantity}件",
                "product": product_name,
                "change": quantity_change,
                "current_stock": result.stock_quantity,
            }
        return {"success": False, "message": "库存更新失败"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 渠道管理
# ================================================================


def list_channels() -> dict:
    """列出所有引流渠道。

    Returns:
        渠道列表
    """
    db = _get_db()
    try:
        channels = db.get_channel_list()
        return {
            "success": True,
            "message": f"共有{len(channels)}个引流渠道",
            "channels": channels,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def add_channel(
    name: str,
    channel_type: str = "external",
    commission_rate: Optional[float] = None,
) -> dict:
    """添加引流渠道。

    Args:
        name: 渠道名称（必填）
        channel_type: 渠道类型，"internal"（内部）、"external"（外部合作）、"platform"（平台）（可选，默认external）
        commission_rate: 提成率（百分比）（可选）

    Returns:
        操作结果
    """
    db = _get_db()
    try:
        channel = db.channels.get_or_create(
            name, channel_type, None, commission_rate
        )
        return {
            "success": True,
            "message": f"✅ 已添加渠道：{name}（{channel_type}）",
            "channel_id": channel.id,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 统计查询
# ================================================================


def query_daily_summary(date_str: Optional[str] = None) -> dict:
    """查询指定日期的收入统计汇总。

    Args:
        date_str: 日期，格式YYYY-MM-DD，默认今天（可选）

    Returns:
        当天的服务收入、产品收入、提成支出和净收入
    """
    db = _get_db()
    try:
        from database.models import ServiceRecord, ProductSale
        from sqlalchemy import func

        query_date = _parse_date(date_str)

        with db.get_session() as session:
            svc = session.query(
                func.count(ServiceRecord.id).label("count"),
                func.coalesce(func.sum(ServiceRecord.amount), 0).label("total"),
                func.coalesce(func.sum(ServiceRecord.commission_amount), 0).label("commission"),
                func.coalesce(func.sum(ServiceRecord.net_amount), 0).label("net"),
            ).filter(ServiceRecord.service_date == query_date).first()

            prod = session.query(
                func.count(ProductSale.id).label("count"),
                func.coalesce(func.sum(ProductSale.total_amount), 0).label("total"),
            ).filter(ProductSale.sale_date == query_date).first()

            records = db.get_daily_records(query_date)

        return {
            "success": True,
            "date": str(query_date),
            "service": {
                "count": svc.count,
                "revenue": float(svc.total),
                "commission": float(svc.commission),
                "net": float(svc.net),
            },
            "product": {
                "count": prod.count,
                "revenue": float(prod.total),
            },
            "total_revenue": float(svc.total) + float(prod.total),
            "total_commission": float(svc.commission),
            "total_net": float(svc.net) + float(prod.total),
            "records": records[:20],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def query_date_range_summary(
    start_date: str,
    end_date: str,
) -> dict:
    """查询日期范围内的收入统计。

    Args:
        start_date: 开始日期，格式YYYY-MM-DD（必填）
        end_date: 结束日期，格式YYYY-MM-DD（必填）

    Returns:
        日期范围内的统计汇总
    """
    db = _get_db()
    try:
        from database.models import ServiceRecord, ProductSale
        from sqlalchemy import func

        start = _parse_date(start_date)
        end = _parse_date(end_date)

        with db.get_session() as session:
            svc = session.query(
                func.count(ServiceRecord.id).label("count"),
                func.coalesce(func.sum(ServiceRecord.amount), 0).label("total"),
                func.coalesce(func.sum(ServiceRecord.commission_amount), 0).label("commission"),
                func.coalesce(func.sum(ServiceRecord.net_amount), 0).label("net"),
            ).filter(
                ServiceRecord.service_date >= start,
                ServiceRecord.service_date <= end,
            ).first()

            prod = session.query(
                func.count(ProductSale.id).label("count"),
                func.coalesce(func.sum(ProductSale.total_amount), 0).label("total"),
            ).filter(
                ProductSale.sale_date >= start,
                ProductSale.sale_date <= end,
            ).first()

        return {
            "success": True,
            "period": f"{start} ~ {end}",
            "service": {
                "count": svc.count,
                "revenue": float(svc.total),
                "commission": float(svc.commission),
                "net": float(svc.net),
            },
            "product": {
                "count": prod.count,
                "revenue": float(prod.total),
            },
            "total_revenue": float(svc.total) + float(prod.total),
            "total_commission": float(svc.commission),
            "total_net": float(svc.net) + float(prod.total),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def query_employee_commission(
    employee_name: Optional[str] = None,
    date_str: Optional[str] = None,
) -> dict:
    """查询员工/技师提成统计。

    Args:
        employee_name: 员工姓名（可选，不填则查询所有员工）
        date_str: 日期，格式YYYY-MM-DD（可选，不填则查询所有日期）

    Returns:
        员工提成统计
    """
    db = _get_db()
    try:
        from database.models import ServiceRecord, ReferralChannel
        from sqlalchemy import func

        with db.get_session() as session:
            query = session.query(
                ReferralChannel.name.label("employee"),
                func.count(ServiceRecord.id).label("count"),
                func.coalesce(func.sum(ServiceRecord.commission_amount), 0).label("total_commission"),
                func.coalesce(func.sum(ServiceRecord.amount), 0).label("total_revenue"),
            ).join(
                ServiceRecord,
                ServiceRecord.referral_channel_id == ReferralChannel.id,
            ).filter(
                ReferralChannel.channel_type == "internal",
            )

            if employee_name:
                query = query.filter(ReferralChannel.name == employee_name)
            if date_str:
                qd = _parse_date(date_str)
                query = query.filter(ServiceRecord.service_date == qd)

            query = query.group_by(ReferralChannel.name)
            results = query.all()

            commissions = []
            total = 0.0
            for r in results:
                amt = float(r.total_commission)
                commissions.append({
                    "employee": r.employee,
                    "service_count": r.count,
                    "commission": amt,
                    "total_revenue": float(r.total_revenue),
                })
                total += amt

        return {
            "success": True,
            "date": date_str or "所有日期",
            "employees": commissions,
            "total_commission": total,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def query_customer_history(
    customer_name: str,
    limit: int = 10,
) -> dict:
    """查询顾客的历史消费记录。

    Args:
        customer_name: 顾客姓名（必填）
        limit: 返回记录条数，默认10条

    Returns:
        顾客消费历史
    """
    db = _get_db()
    try:
        from database.models import Customer, ServiceRecord, ProductSale

        with db.get_session() as session:
            customer = session.query(Customer).filter(
                Customer.name == customer_name
            ).first()

            if not customer:
                return {"success": False, "message": f"未找到顾客：{customer_name}"}

            services = session.query(ServiceRecord).filter(
                ServiceRecord.customer_id == customer.id
            ).order_by(ServiceRecord.service_date.desc()).limit(limit).all()

            service_history = [{
                "id": s.id,
                "date": str(s.service_date),
                "service": s.service_type.name if s.service_type else "未知",
                "amount": float(s.amount),
                "notes": s.notes,
            } for s in services]

            products = session.query(ProductSale).filter(
                ProductSale.customer_id == customer.id
            ).order_by(ProductSale.sale_date.desc()).limit(limit).all()

            product_history = [{
                "id": p.id,
                "date": str(p.sale_date),
                "product": p.product.name if p.product else "未知",
                "amount": float(p.total_amount),
                "quantity": p.quantity,
            } for p in products]

        return {
            "success": True,
            "customer": customer_name,
            "service_records": service_history,
            "product_records": product_history,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def query_low_stock_products() -> dict:
    """查询低库存产品，方便及时补货。

    Returns:
        低库存产品列表
    """
    db = _get_db()
    try:
        products = db.products.get_low_stock()
        result = [{
            "id": p.id,
            "name": p.name,
            "stock_quantity": p.stock_quantity,
            "low_stock_threshold": p.low_stock_threshold,
        } for p in products]

        return {
            "success": True,
            "message": f"有{len(result)}种产品库存偏低",
            "products": result,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ================================================================
# 业务配置查看
# ================================================================


def get_business_overview() -> dict:
    """获取当前业务概览（服务类型、产品、员工、渠道的汇总信息）。

    Returns:
        业务概览信息
    """
    db = _get_db()
    try:
        from database.models import ServiceType, Product, Employee, ReferralChannel, Customer, Membership

        with db.get_session() as session:
            service_count = session.query(ServiceType).count()
            product_count = session.query(Product).count()
            staff_count = session.query(Employee).filter(Employee.is_active == True).count()
            customer_count = session.query(Customer).count()
            active_membership_count = session.query(Membership).filter(Membership.is_active == True).count()
            channel_count = session.query(ReferralChannel).filter(ReferralChannel.is_active == True).count()

        return {
            "success": True,
            "overview": {
                "service_types": service_count,
                "products": product_count,
                "active_staff": staff_count,
                "customers": customer_count,
                "active_memberships": active_membership_count,
                "channels": channel_count,
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}



# ================================================================
# 预约相关
# ================================================================


def create_appointment(
    customer_name: str,
    service_name: str,
    appointment_date: Optional[str] = None,
    appointment_time: Optional[str] = None,
    phone: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """创建一条预约记录。

    顾客说"明天下午两点剪个发"时调用。日期必须转成 YYYY-MM-DD（今天日期
    见系统提示）；时间统一转成 24小时制 HH:MM（如 14:30）。

    Args:
        customer_name: 顾客姓名（必填）。
        service_name: 预约的服务项目（必填，如"男士剪发"）。
        appointment_date: 预约日期 YYYY-MM-DD，不传视为今天。
        appointment_time: 预约时间 HH:MM（可选）。
        phone: 联系电话（可选）。
        notes: 备注（可选）。
    """
    try:
        d = _parse_date(appointment_date)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    if d < date.today():
        return {"success": False,
                "error": f"不能预约过去的日期（{d.isoformat()}），请和顾客确认正确日期后重试"}
    name = str(customer_name).strip() if customer_name else ""
    if not name:
        return {"success": False, "error": "顾客姓名不能为空"}
    service = str(service_name).strip() if service_name else ""
    if not service:
        return {"success": False, "error": "预约的服务项目不能为空"}
    db = _get_db()
    # 时段冲突检测：同日期+同时间已有未取消的预约则拒绝（防撞单）
    if appointment_time:
        conflict = db.list_appointments(target_date=d, status="pending") + \
                   db.list_appointments(target_date=d, status="confirmed")
        conflict = [c for c in conflict
                    if (c.appointment_time or "") == str(appointment_time).strip()]
        if conflict:
            c = conflict[0]
            return {"success": False,
                    "error": f"{d.isoformat()} {appointment_time} 已有预约"
                             f"（{c.customer_name}的{c.service_name}），"
                             f"请和顾客商量改到其他时间"}
    appt = db.create_appointment(
        customer_name=name,
        service_name=service,
        appointment_date=d,
        appointment_time=appointment_time,
        phone=phone,
        notes=notes,
    )
    logger.info(f"预约已创建: id={appt.id} {name} {service} {d} {appointment_time or ''}")
    return {
        "success": True,
        "appointment_id": appt.id,
        "customer_name": appt.customer_name,
        "service_name": appt.service_name,
        "appointment_date": appt.appointment_date.isoformat(),
        "appointment_time": appt.appointment_time,
        "status": appt.status,
        "message": f"已为{name}预约{appt.appointment_date.isoformat()} "
                   f"{appt.appointment_time or ''}的{service}",
    }


def list_appointments(
    date_str: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """查询预约列表。

    顾客或店主问"明天有什么预约"时调用。不传日期则查今天起的 upcoming。

    Args:
        date_str: 查询某天的预约 YYYY-MM-DD（可选）。
        status: 按状态过滤：pending/confirmed/completed/cancelled/no_show（可选）。
    """
    db = _get_db()
    try:
        from datetime import timedelta
        if date_str:
            d = _parse_date(date_str)
            appts = db.list_appointments(target_date=d, status=status)
        else:
            appts = db.list_appointments(
                start_date=date.today(),
                end_date=date.today() + timedelta(days=30),
                status=status,
            )
        items = [{
            "id": a.id,
            "customer_name": a.customer_name,
            "service_name": a.service_name,
            "date": a.appointment_date.isoformat(),
            "time": a.appointment_time,
            "status": a.status,
            "phone": a.phone,
        } for a in appts]
        return {"success": True, "count": len(items), "appointments": items}
    except ValueError as e:
        return {"success": False, "error": str(e)}


def update_appointment_status(appointment_id: int, new_status: str) -> dict:
    """更新预约状态。

    顾客确认到店、取消预约、爽约时调用。状态只能按合法路径流转：
    待确认→已确认→已完成；任意未完成状态可取消/标爽约；终态不可回退。

    Args:
        appointment_id: 预约ID（必填）。
        new_status: 新状态（必填）：confirmed=已确认 / completed=已完成 /
                    cancelled=已取消 / no_show=爽约。
    """
    db = _get_db()
    try:
        appt = db.appointments.update_status(int(appointment_id), str(new_status).strip())
    except (ValueError, TypeError) as e:
        return {"success": False, "error": f"状态更新失败: {e}"}
    if not appt:
        return {"success": False, "error": f"未找到ID为{appointment_id}的预约"}
    logger.info(f"预约状态更新: id={appt.id} -> {appt.status}")
    return {
        "success": True,
        "appointment_id": appt.id,
        "customer_name": appt.customer_name,
        "service_name": appt.service_name,
        "date": appt.appointment_date.isoformat(),
        "status": appt.status,
    }
