"""Web 管理平台 - 聊天 + 数据库可视化

提供一个现代化的 Web 管理平台，包含：
1. 与 Agent 对话的聊天界面
2. 数据库可视化仪表盘（员工、顾客、服务记录、销售、会员等）
3. 登录认证（支持外网安全访问）

使用方式：
    ```python
    channel = WebChannel(message_handler=agent_handler, port=8080)
    await channel.startup()
    # 访问 http://localhost:8080 开始使用
    ```
"""
import asyncio
import hashlib
import json
import secrets
import signal as signal_module
import threading
import time
import uuid
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from loguru import logger

from interface.base import Channel, Message, MessageHandler, MessageType, Reply


def _json_serial(obj):
    """JSON 序列化辅助函数，处理特殊类型"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


class WebChannel(Channel):
    """Web 管理平台通道

    基于 FastAPI 提供完整的 Web 管理界面，包含：
    - 聊天界面：与 Agent 实时对话
    - 数据仪表盘：可视化查看数据库中的所有业务数据
    - 登录认证：用户名/密码保护，支持外网安全访问

    路由：
    - GET  /             → 登录页面 / 主应用
    - POST /api/login    → 登录认证
    - POST /api/chat     → 聊天 API
    - GET  /api/dashboard → 仪表盘数据
    - GET  /api/employees → 员工列表
    - GET  /api/customers → 顾客列表
    - GET  /api/services  → 服务记录
    - GET  /api/sales     → 销售记录
    - GET  /api/memberships → 会员卡
    - GET  /health        → 健康检查
    """

    def __init__(
        self,
        message_handler: Optional[MessageHandler] = None,
        host: str = "0.0.0.0",
        port: int = 8080,
        username: str = "admin",
        password: str = "admin123",
        secret_key: str = "change-me-to-a-random-secret-key",
        db_manager=None,
    ):
        super().__init__("web", message_handler)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.secret_key = secret_key
        self.db_manager = db_manager
        self.app = None
        self._server_thread: Optional[threading.Thread] = None
        self._server = None  # uvicorn.Server 实例
        self._server_loop = None  # 服务器事件循环
        # 简易 token 存储
        self._valid_tokens: Dict[str, datetime] = {}
        # 登录防爆破：IP → 失败时间戳列表；5 次失败锁 10 分钟
        self._login_attempts: Dict[str, List[float]] = {}
        self._login_lock_secs: float = 600.0
        self._login_max_attempts: int = 5

    def _login_blocked(self, ip: str) -> float:
        """检查 IP 是否被锁定。返回剩余锁定秒数，0 表示未锁。"""
        now = time.time()
        fails = [t for t in self._login_attempts.get(ip, [])
                 if now - t < self._login_lock_secs]
        self._login_attempts[ip] = fails
        if len(fails) >= self._login_max_attempts:
            oldest = min(fails)
            return max(0.0, self._login_lock_secs - (now - oldest))
        return 0.0

    def _record_login_fail(self, ip: str) -> None:
        """记录一次登录失败。"""
        now = time.time()
        fails = [t for t in self._login_attempts.get(ip, [])
                 if now - t < self._login_lock_secs]
        fails.append(now)
        self._login_attempts[ip] = fails
        if len(fails) >= self._login_max_attempts:
            logger.warning(f"[Web] 登录失败达上限，IP {ip} 已锁定10分钟")

    def _record_login_success(self, ip: str) -> None:
        """登录成功，清除该 IP 失败记录。"""
        self._login_attempts.pop(ip, None)

    def _generate_token(self) -> str:
        """生成登录 token"""
        token = secrets.token_hex(32)
        self._valid_tokens[token] = datetime.now() + timedelta(hours=24)
        return token

    def _verify_token(self, token: str) -> bool:
        """验证 token"""
        if token not in self._valid_tokens:
            return False
        if datetime.now() > self._valid_tokens[token]:
            del self._valid_tokens[token]
            return False
        return True

    def _create_app(self):
        """创建 FastAPI 应用"""
        from fastapi import FastAPI, Request, Depends, HTTPException
        from fastapi.responses import HTMLResponse, JSONResponse

        app = FastAPI(
            title="美业AI店长助手",
            description="Web 管理平台 - 聊天 + 数据库可视化",
            version="3.0.0",
        )

        def get_current_user(request: Request):
            """从请求头中验证 token"""
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                if self._verify_token(token):
                    return True
            raise HTTPException(status_code=401, detail="未授权，请先登录")

        # ==================== 页面路由 ====================

        @app.get("/", response_class=HTMLResponse)
        async def index():
            """主页面（SPA）"""
            return APP_HTML

        # ==================== 认证 API ====================

        @app.post("/api/login")
        async def login(request: Request, data: dict):
            """登录认证（带防爆破：5次失败锁定10分钟）"""
            client_ip = request.client.host if request.client else "unknown"
            blocked = self._login_blocked(client_ip)
            if blocked > 0:
                return JSONResponse(
                    status_code=429,
                    content={"success": False,
                             "error": f"尝试次数过多，请 {int(blocked // 60) + 1} 分钟后再试"},
                )
            username = data.get("username", "")
            password = data.get("password", "")
            if username == self.username and password == self.password:
                self._record_login_success(client_ip)
                token = self._generate_token()
                return {"success": True, "token": token}
            self._record_login_fail(client_ip)
            logger.warning(f"[Web] 登录失败 user={username!r} ip={client_ip}")
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "用户名或密码错误"},
            )

        # ==================== 聊天 API ====================

        @app.post("/api/chat")
        async def chat_api(data: dict, _=Depends(get_current_user)):
            """聊天 API"""
            content = data.get("content", "").strip()
            if not content:
                return JSONResponse(
                    status_code=400,
                    content={"error": "消息内容不能为空"},
                )

            session_id = data.get("session_id", str(uuid.uuid4()))
            sender_name = data.get("sender_name", "Web用户")

            message = Message(
                type=MessageType.TEXT,
                content=content,
                sender_id=f"web_{session_id}",
                sender_name=sender_name,
                session_id=session_id,
                timestamp=datetime.now(),
            )

            try:
                reply = await self.handle(message)
                if reply:
                    return {"reply": reply.content, "type": reply.type.value}
                else:
                    return {"reply": "抱歉，我暂时无法处理你的请求。", "type": "text"}
            except Exception as e:
                # 对外只给通用话术，内部异常细节进日志（防泄漏路径/SQL/配置）
                logger.exception(f"Web 聊天处理出错 [session={session_id}]: {e}")
                return JSONResponse(
                    status_code=500,
                    content={"error": "哎呀，我这边出了点小状况，请稍后再试 🙏"},
                )

        # ==================== 数据库 API ====================

        @app.get("/api/dashboard")
        async def dashboard_data(_=Depends(get_current_user)):
            """仪表盘概览数据"""
            if not self.db_manager:
                return {"error": "数据库未连接"}

            db = self.db_manager
            today = date.today()
            try:
                today_records = db.get_daily_records(today)
                staff_list = db.get_staff_list()

                today_revenue = sum(
                    float(r.get("amount", 0)) for r in today_records
                )
                today_count = len(today_records)

                # 最近7天数据
                weekly_data = []
                for i in range(6, -1, -1):
                    d = today - timedelta(days=i)
                    records = db.get_daily_records(d)
                    revenue = sum(float(r.get("amount", 0)) for r in records)
                    weekly_data.append({
                        "date": d.isoformat(),
                        "revenue": revenue,
                        "count": len(records),
                    })

                # 数据备份状态（每天自动备份，给店主安全感也是卖点）
                backup_info = None
                try:
                    from database.backup import get_backup_status
                    backup_info = get_backup_status()
                except Exception:
                    pass

                return {
                    "today_revenue": today_revenue,
                    "today_count": today_count,
                    "staff_count": len(staff_list),
                    "weekly_data": weekly_data,
                    "backup": backup_info,
                }
            except Exception as e:
                logger.error(f"获取仪表盘数据出错: {e}")
                return {"error": str(e)}

        @app.get("/api/ai_contributions")
        async def ai_contributions(_=Depends(get_current_user)):
            """AI店长本月功劳簿 —— 把产品价值量化透出给老板"""
            if not self.db_manager:
                return {"error": "数据库未连接"}
            from database.models import (
                ServiceRecord, ProductSale, Membership, Product,
            )
            db = self.db_manager
            today = date.today()
            month_start = today.replace(day=1)
            try:
                with db.get_session() as session:
                    # 本月记录的经营流水（服务+产品销售）
                    svc_count = session.query(ServiceRecord).filter(
                        ServiceRecord.service_date >= month_start
                    ).count()
                    sale_count = session.query(ProductSale).filter(
                        ProductSale.sale_date >= month_start
                    ).count()
                    total_records = svc_count + sale_count

                    # 本月新增会员卡数
                    new_members = session.query(Membership).filter(
                        Membership.opened_at >= month_start
                    ).count()

                    # 30天内到期且有余额的会员卡（涉及余额=召回机会）
                    expiring_soon = 0
                    expiring_balance = 0.0
                    for m in session.query(Membership).filter(
                        Membership.is_active == True  # noqa: E712
                    ).all():
                        if m.expires_at and month_start <= m.expires_at <= today + timedelta(days=30):
                            expiring_soon += 1
                            expiring_balance += float(m.balance or 0)

                    # 低库存产品数
                    low_stock = session.query(Product).filter(
                        Product.stock_quantity <= Product.low_stock_threshold
                    ).count()

                # 折算省时：每笔流水约1分钟手工记账时间
                minutes_saved = total_records
                return {
                    "month_records": total_records,
                    "minutes_saved": minutes_saved,
                    "new_members": new_members,
                    "expiring_soon": expiring_soon,
                    "expiring_balance": round(expiring_balance, 2),
                    "low_stock": low_stock,
                    "month": today.strftime("%Y-%m"),
                }
            except Exception as e:
                logger.error(f"获取功劳簿数据出错: {e}")
                return {"error": str(e)}

        @app.get("/api/employees")
        async def employees_list(_=Depends(get_current_user)):
            """员工列表"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                data = self.db_manager.get_staff_list(active_only=False)
                return {"data": data}
            except Exception as e:
                logger.error(f"获取员工列表出错: {e}")
                return {"data": [], "error": str(e)}

        @app.get("/api/knowledge_base")
        async def knowledge_base_stats(_=Depends(get_current_user)):
            """知识库统计"""
            try:
                from agent.agent import get_knowledge_base
                kb = get_knowledge_base()
                if kb:
                    return {"enabled": True, "stats": kb.get_stats()}
                return {"enabled": False, "stats": {}}
            except Exception as e:
                logger.error(f"获取知识库统计出错: {e}")
                return {"enabled": False, "error": str(e)}

        @app.get("/api/knowledge_base/search")
        async def knowledge_base_search(q: str = "", _=Depends(get_current_user)):
            """知识库搜索"""
            try:
                from agent.agent import get_knowledge_base
                kb = get_knowledge_base()
                if kb and q:
                    results = kb.search(q)
                    return {"results": results}
                return {"results": []}
            except Exception as e:
                logger.error(f"知识库搜索出错: {e}")
                return {"results": [], "error": str(e)}

        @app.get("/api/knowledge_base/items")
        async def knowledge_base_items(
            category: str = "", _=Depends(get_current_user)
        ):
            """知识库条目列表"""
            try:
                from agent.agent import get_knowledge_base
                kb = get_knowledge_base()
                if kb:
                    cat = category if category else None
                    items = kb.list_items(cat)
                    return {"items": items}
                return {"items": []}
            except Exception as e:
                logger.error(f"获取知识库条目出错: {e}")
                return {"items": [], "error": str(e)}

        @app.post("/api/knowledge_base/items")
        async def knowledge_base_add(request: Request, _=Depends(get_current_user)):
            """新增知识库条目"""
            try:
                from agent.agent import get_knowledge_base
                kb = get_knowledge_base()
                if not kb:
                    return {"success": False, "error": "知识库未加载"}
                body = await request.json()
                category = body.get("category", "")
                item = body.get("item", {})
                ok = kb.add_item(category, item)
                return {"success": ok}
            except Exception as e:
                logger.error(f"新增知识库条目出错: {e}")
                return {"success": False, "error": str(e)}

        @app.put("/api/knowledge_base/items")
        async def knowledge_base_update(request: Request, _=Depends(get_current_user)):
            """更新知识库条目"""
            try:
                from agent.agent import get_knowledge_base
                kb = get_knowledge_base()
                if not kb:
                    return {"success": False, "error": "知识库未加载"}
                body = await request.json()
                category = body.get("category", "")
                index = body.get("index", -1)
                item = body.get("item", {})
                ok = kb.update_item(category, index, item)
                return {"success": ok}
            except Exception as e:
                logger.error(f"更新知识库条目出错: {e}")
                return {"success": False, "error": str(e)}

        @app.delete("/api/knowledge_base/items")
        async def knowledge_base_delete(
            category: str = "", index: int = -1, _=Depends(get_current_user)
        ):
            """删除知识库条目"""
            try:
                from agent.agent import get_knowledge_base
                kb = get_knowledge_base()
                if not kb:
                    return {"success": False, "error": "知识库未加载"}
                ok = kb.delete_item(category, index)
                return {"success": ok}
            except Exception as e:
                logger.error(f"删除知识库条目出错: {e}")
                return {"success": False, "error": str(e)}

        @app.get("/api/customers")
        async def customers_list(_=Depends(get_current_user)):
            """顾客列表"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                from database.models import Customer
                session = self.db_manager.get_session()
                customers = session.query(Customer).order_by(
                    Customer.created_at.desc()
                ).limit(200).all()
                data = [
                    {
                        "id": c.id,
                        "name": c.name,
                        "phone": c.phone,
                        "notes": c.notes,
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                    }
                    for c in customers
                ]
                session.close()
                return {"data": data}
            except Exception as e:
                logger.error(f"获取顾客列表出错: {e}")
                return {"data": [], "error": str(e)}

        @app.get("/api/services")
        async def services_list(
            start_date: Optional[str] = None,
            end_date: Optional[str] = None,
            _=Depends(get_current_user),
        ):
            """服务记录列表"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                from database.models import ServiceRecord, Customer, Employee, ServiceType
                session = self.db_manager.get_session()
                query = session.query(ServiceRecord).order_by(
                    ServiceRecord.service_date.desc(),
                    ServiceRecord.created_at.desc(),
                )
                if start_date:
                    query = query.filter(
                        ServiceRecord.service_date >= datetime.strptime(start_date, "%Y-%m-%d").date()
                    )
                if end_date:
                    query = query.filter(
                        ServiceRecord.service_date <= datetime.strptime(end_date, "%Y-%m-%d").date()
                    )
                records = query.limit(500).all()
                data = []
                for r in records:
                    data.append({
                        "id": r.id,
                        "service_date": r.service_date.isoformat() if r.service_date else None,
                        "customer_name": r.customer.name if r.customer else None,
                        "employee_name": r.employee.name if r.employee else None,
                        "service_type": r.service_type.name if r.service_type else None,
                        "amount": float(r.amount) if r.amount else 0,
                        "commission_amount": float(r.commission_amount) if r.commission_amount else 0,
                        "net_amount": float(r.net_amount) if r.net_amount else None,
                        "notes": r.notes,
                        "confirmed": r.confirmed,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    })
                session.close()
                return {"data": data}
            except Exception as e:
                logger.error(f"获取服务记录出错: {e}")
                return {"data": [], "error": str(e)}

        @app.get("/api/sales")
        async def sales_list(
            start_date: Optional[str] = None,
            end_date: Optional[str] = None,
            _=Depends(get_current_user),
        ):
            """商品销售记录"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                from database.models import ProductSale
                session = self.db_manager.get_session()
                query = session.query(ProductSale).order_by(
                    ProductSale.sale_date.desc(),
                    ProductSale.created_at.desc(),
                )
                if start_date:
                    query = query.filter(
                        ProductSale.sale_date >= datetime.strptime(start_date, "%Y-%m-%d").date()
                    )
                if end_date:
                    query = query.filter(
                        ProductSale.sale_date <= datetime.strptime(end_date, "%Y-%m-%d").date()
                    )
                records = query.limit(500).all()
                data = []
                for r in records:
                    data.append({
                        "id": r.id,
                        "sale_date": r.sale_date.isoformat() if r.sale_date else None,
                        "product_name": r.product.name if r.product else None,
                        "customer_name": r.customer.name if r.customer else None,
                        "quantity": r.quantity,
                        "unit_price": float(r.unit_price) if r.unit_price else None,
                        "total_amount": float(r.total_amount) if r.total_amount else 0,
                        "notes": r.notes,
                        "confirmed": r.confirmed,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    })
                session.close()
                return {"data": data}
            except Exception as e:
                logger.error(f"获取销售记录出错: {e}")
                return {"data": [], "error": str(e)}

        @app.get("/api/memberships")
        async def memberships_list(_=Depends(get_current_user)):
            """会员卡列表"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                from database.models import Membership
                session = self.db_manager.get_session()
                memberships = session.query(Membership).order_by(
                    Membership.created_at.desc()
                ).limit(200).all()
                data = []
                for m in memberships:
                    data.append({
                        "id": m.id,
                        "customer_name": m.customer.name if m.customer else None,
                        "card_type": m.card_type,
                        "total_amount": float(m.total_amount) if m.total_amount else 0,
                        "balance": float(m.balance) if m.balance else 0,
                        "remaining_sessions": m.remaining_sessions,
                        "opened_at": m.opened_at.isoformat() if m.opened_at else None,
                        "expires_at": m.expires_at.isoformat() if m.expires_at else None,
                        "points": m.points or 0,
                        "is_active": m.is_active,
                    })
                session.close()
                return {"data": data}
            except Exception as e:
                logger.error(f"获取会员卡列表出错: {e}")
                return {"data": [], "error": str(e)}

        @app.get("/api/products")
        async def products_list(_=Depends(get_current_user)):
            """商品列表"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                from database.models import Product
                session = self.db_manager.get_session()
                products = session.query(Product).order_by(
                    Product.created_at.desc()
                ).limit(200).all()
                data = [
                    {
                        "id": p.id,
                        "name": p.name,
                        "category": p.category,
                        "unit_price": float(p.unit_price) if p.unit_price else None,
                        "stock_quantity": p.stock_quantity,
                        "low_stock_threshold": p.low_stock_threshold,
                    }
                    for p in products
                ]
                session.close()
                return {"data": data}
            except Exception as e:
                logger.error(f"获取商品列表出错: {e}")
                return {"data": [], "error": str(e)}

        @app.get("/api/channels")
        async def channels_list(_=Depends(get_current_user)):
            """引流渠道列表"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                data = self.db_manager.get_channel_list()
                return {"data": data}
            except Exception as e:
                logger.error(f"获取渠道列表出错: {e}")
                return {"data": [], "error": str(e)}

        # ==================== 预约管理 ====================

        @app.get("/api/appointments")
        async def appointments_list(
            date: str = "", status: str = "", _=Depends(get_current_user)
        ):
            """预约列表（可按日期/状态过滤）"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                from datetime import date as _date, timedelta
                if date:
                    d = _date.fromisoformat(date)
                    rows = self.db_manager.list_appointments(target_date=d)
                else:
                    today = _date.today()
                    rows = self.db_manager.list_appointments(
                        start_date=today, end_date=today + timedelta(days=30)
                    )
                if status:
                    rows = [r for r in rows if r.status == status]
                data = [{
                    "id": r.id,
                    "customer_name": r.customer_name,
                    "phone": r.phone or "",
                    "service_name": r.service_name,
                    "date": r.appointment_date.isoformat(),
                    "time": r.appointment_time or "",
                    "status": r.status,
                    "notes": r.notes or "",
                } for r in rows]
                return {"data": data}
            except Exception as e:
                logger.error(f"获取预约列表出错: {e}")
                return {"data": [], "error": str(e)}

        @app.put("/api/appointments/{appointment_id}/status")
        async def appointment_update_status(
            appointment_id: int, payload: dict, _=Depends(get_current_user)
        ):
            """更新预约状态（店主手动确认/取消）"""
            if not self.db_manager:
                return {"success": False, "error": "数据库未连接"}
            try:
                new_status = str(payload.get("status", "")).strip()
                appt = self.db_manager.appointments.update_status(
                    appointment_id, new_status
                )
                if not appt:
                    return {"success": False, "error": "预约不存在"}
                return {"success": True, "status": appt.status}
            except Exception as e:
                logger.error(f"更新预约状态出错: {e}")
                return {"success": False, "error": str(e)}

        # ==================== 顾客消息流 ====================

        @app.get("/api/chat_sessions")
        async def chat_sessions(_=Depends(get_current_user)):
            """会话列表（每个会话概要+待人工标记数）"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                return {"data": self.db_manager.get_chat_sessions(limit=30)}
            except Exception as e:
                logger.error(f"获取会话列表出错: {e}")
                return {"data": [], "error": str(e)}

        @app.get("/api/chat_messages")
        async def chat_messages(
            session_id: str = "", limit: int = 100, _=Depends(get_current_user)
        ):
            """会话消息流（时间正序）"""
            if not self.db_manager:
                return {"data": [], "error": "数据库未连接"}
            try:
                rows = self.db_manager.get_chat_messages(
                    session_id=session_id or None, limit=limit
                )
                return {"data": [{
                    "id": r.id,
                    "session_id": r.session_id,
                    "direction": r.direction,
                    "sender_name": r.sender_name or "",
                    "content": r.content,
                    "needs_human": bool(r.needs_human),
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                } for r in rows]}
            except Exception as e:
                logger.error(f"获取会话消息出错: {e}")
                return {"data": [], "error": str(e)}

        # ==================== 健康检查 ====================

        @app.get("/health")
        async def health_check():
            """健康检查"""
            return {
                "status": "ok",
                "channel": "web",
                "running": self.running,
                "db_connected": self.db_manager is not None,
            }

        return app

    @staticmethod
    def _cleanup_port(port: int) -> None:
        """清理占用指定端口的残留进程。

        在启动前调用，确保端口可用。仅清理由本应用残留的进程。

        Args:
            port: 需要清理的端口号。
        """
        import subprocess
        import os as _os

        try:
            # 使用 lsof 查找占用端口的进程
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                current_pid = str(_os.getpid())
                for pid_str in pids:
                    pid_str = pid_str.strip()
                    if pid_str and pid_str != current_pid:
                        try:
                            pid = int(pid_str)
                            # 先尝试优雅终止
                            _os.kill(pid, signal_module.SIGTERM)
                            logger.info(f"已终止占用端口 {port} 的残留进程 (PID: {pid})")
                        except (ProcessLookupError, PermissionError):
                            pass
                        except Exception as e:
                            logger.debug(f"清理进程 {pid_str} 时出错: {e}")

                # 等待进程释放端口
                import time
                time.sleep(0.5)

                # 再次检查，如果仍被占用则强制终止
                result2 = subprocess.run(
                    ["lsof", "-ti", f":{port}"],
                    capture_output=True, text=True, timeout=5
                )
                if result2.returncode == 0 and result2.stdout.strip():
                    for pid_str in result2.stdout.strip().split('\n'):
                        pid_str = pid_str.strip()
                        if pid_str and pid_str != current_pid:
                            try:
                                _os.kill(int(pid_str), signal_module.SIGKILL)
                                logger.warning(f"强制终止占用端口 {port} 的进程 (PID: {pid_str})")
                            except (ProcessLookupError, PermissionError):
                                pass
                    time.sleep(0.3)
        except FileNotFoundError:
            # lsof 不可用，尝试使用 fuser
            try:
                result = subprocess.run(
                    ["fuser", "-k", f"{port}/tcp"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    logger.info(f"已通过 fuser 清理端口 {port} 上的残留进程")
                    import time
                    time.sleep(0.5)
            except FileNotFoundError:
                logger.debug("lsof 和 fuser 均不可用，跳过端口清理")
        except Exception as e:
            logger.debug(f"端口清理时出错（不影响启动）: {e}")

    async def startup(self):
        """启动 Web 服务器"""
        import uvicorn

        # 启动前清理残留端口占用
        self._cleanup_port(self.port)

        self.app = self._create_app()
        self.running = True

        def run_server():
            """在独立线程中运行 uvicorn 服务器"""
            import asyncio

            # 创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._server_loop = loop

            # 创建 uvicorn 配置，禁用 uvicorn 自身的信号处理
            # uvicorn 日志显式接到 stderr（冻结环境下默认 logging 可能被吞，bind/lifespan 状态必须可见）
            _uv_log = {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "plain": {"format": "%(levelname)s [uvicorn] %(message)s"}
                },
                "handlers": {
                    "stderr": {
                        "class": "logging.StreamHandler",
                        "stream": "ext://sys.stderr",
                        "formatter": "plain",
                    }
                },
                "loggers": {
                    "uvicorn": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
                    "uvicorn.error": {"level": "INFO"},
                    "uvicorn.access": {"handlers": ["stderr"], "level": "WARNING", "propagate": False},
                },
            }
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                log_level="info",
                log_config=_uv_log,
                loop="asyncio",
            )

            # 创建服务器实例
            self._server = uvicorn.Server(config)
            # 禁用 uvicorn 内置的信号处理器（由 app.py 统一管理）
            self._server.install_signal_handlers = lambda: None

            try:
                # 运行服务器（捕获返回值：False=lifespan 启动失败，正常阻塞运行中不会返回）
                ret = loop.run_until_complete(self._server.serve())
                logger.error(
                    f"[diag] uvicorn serve 已返回: ret={ret}, "
                    f"should_exit={getattr(self._server, 'should_exit', '?')}, "
                    f"started={getattr(self._server, 'started', '?')}"
                )
            except BaseException as e:
                logger.error(f"服务器运行出错: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
            finally:
                # 清理事件循环中的待处理任务
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    pass
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass

        self._server_thread = threading.Thread(target=run_server, daemon=True)
        self._server_thread.start()

        # 等待服务器启动
        max_wait = 5
        waited = 0
        while self._server is None and waited < max_wait:
            await asyncio.sleep(0.1)
            waited += 0.1

        logger.info(f"Web 管理平台已启动: http://{self.host}:{self.port}")

    async def shutdown(self):
        """停止 Web 服务器，确保端口被释放"""
        self.running = False

        if self._server is not None:
            try:
                logger.info("正在停止 Web 服务器...")
                # 设置退出标志
                self._server.should_exit = True

                # 等待服务器线程自然退出（最多 3 秒）
                if self._server_thread and self._server_thread.is_alive():
                    self._server_thread.join(timeout=3.0)

                # 如果仍未退出，强制终止
                if self._server_thread and self._server_thread.is_alive():
                    logger.warning("服务器未在 3 秒内优雅停止，强制退出...")
                    self._server.force_exit = True

                    # 在服务器事件循环中停止
                    if self._server_loop and self._server_loop.is_running():
                        self._server_loop.call_soon_threadsafe(
                            self._server_loop.stop
                        )

                    self._server_thread.join(timeout=2.0)
                    if self._server_thread.is_alive():
                        logger.warning("服务器线程未能停止，将随主进程退出")
            except Exception as e:
                logger.error(f"停止服务器时出错: {e}")
            finally:
                self._server = None
                self._server_loop = None
                self._server_thread = None

        logger.info("Web 管理平台已停止")

    async def send(self, session_id: str, reply: Reply):
        """发送回复（Web 通道通过 HTTP 响应返回）"""
        logger.debug(f"Web 发送: session={session_id}, content={reply.content[:50]}")


# ==================== 前端 SPA HTML ====================

APP_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>美业AI店长助手</title>
    <style>
        :root {
            --primary: #e11d48;
            --primary-light: #fb7185;
            --primary-dark: #9f1239;
            --bg: #fdf2f8;
            --card: #ffffff;
            --text: #1e293b;
            --text-secondary: #64748b;
            --border: #fce7f3;
            --success: #22c55e;
            --warning: #f59e0b;
            --danger: #ef4444;
            --sidebar-width: 240px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
            background: var(--bg);
            color: var(--text);
            height: 100vh;
            overflow: hidden;
        }

        /* ===== 登录页 ===== */
        .login-page {
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            background: linear-gradient(135deg, #e11d48 0%, #be185d 50%, #9f1239 100%);
        }
        .login-card {
            background: white;
            border-radius: 16px;
            padding: 48px 40px;
            width: 400px;
            box-shadow: 0 25px 50px rgba(0,0,0,0.15);
        }
        .login-card h1 {
            font-size: 24px;
            text-align: center;
            margin-bottom: 4px;
            color: var(--primary-dark);
        }
        .login-card p {
            text-align: center;
            color: var(--text-secondary);
            margin-bottom: 32px;
            font-size: 14px;
        }
        .login-card .brand-sub {
            text-align: center;
            font-size: 12px;
            color: var(--primary-light);
            margin-bottom: 24px;
            letter-spacing: 2px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }
        .form-group input {
            width: 100%;
            padding: 12px 16px;
            border: 1px solid var(--border);
            border-radius: 10px;
            font-size: 15px;
            outline: none;
            transition: border-color 0.2s;
        }
        .form-group input:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79,70,229,0.1);
        }
        .login-btn {
            width: 100%;
            padding: 14px;
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }
        .login-btn:hover { background: var(--primary-dark); }
        .login-error {
            color: var(--danger);
            text-align: center;
            font-size: 13px;
            margin-top: 12px;
            min-height: 20px;
        }

        /* ===== 主布局 ===== */
        .app-layout {
            display: none;
            height: 100vh;
        }
        .app-layout.active { display: flex; }

        /* 侧边栏 */
        .sidebar {
            width: var(--sidebar-width);
            background: var(--primary-dark);
            color: white;
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
        }
        .sidebar-header {
            padding: 20px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        .sidebar-header h2 {
            font-size: 18px;
            font-weight: 700;
        }
        .sidebar-header small {
            opacity: 0.7;
            font-size: 12px;
        }
        .sidebar-nav {
            flex: 1;
            padding: 12px 0;
            overflow-y: auto;
        }
        .nav-item {
            display: flex;
            align-items: center;
            padding: 12px 20px;
            cursor: pointer;
            transition: background 0.15s;
            font-size: 14px;
            gap: 12px;
            border-left: 3px solid transparent;
        }
        .nav-item:hover { background: rgba(255,255,255,0.08); }
        .nav-item.active {
            background: rgba(255,255,255,0.12);
            border-left-color: var(--primary-light);
        }
        .nav-item .icon { font-size: 18px; width: 24px; text-align: center; }
        .sidebar-footer {
            padding: 16px 20px;
            border-top: 1px solid rgba(255,255,255,0.1);
        }
        .logout-btn {
            width: 100%;
            padding: 10px;
            background: rgba(255,255,255,0.1);
            color: white;
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            transition: background 0.2s;
        }
        .logout-btn:hover { background: rgba(255,255,255,0.2); }

        /* 主内容区 */
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .content-header {
            padding: 20px 28px;
            background: var(--card);
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .content-header h1 { font-size: 22px; font-weight: 700; }
        .content-body {
            flex: 1;
            overflow-y: auto;
            padding: 24px 28px;
        }

        /* ===== 仪表盘 ===== */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
            margin-bottom: 28px;
        }
        .stat-card {
            background: var(--card);
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            border: 1px solid var(--border);
        }
        .stat-card .label {
            font-size: 13px;
            color: var(--text-secondary);
            margin-bottom: 8px;
        }
        .stat-card .value {
            font-size: 28px;
            font-weight: 700;
            color: var(--text);
        }
        .stat-card .unit { font-size: 14px; color: var(--text-secondary); margin-left: 4px; }

        /* 图表区域 */
        .chart-card {
            background: var(--card);
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            border: 1px solid var(--border);
            margin-bottom: 24px;
        }
        .chart-card h3 {
            font-size: 16px;
            margin-bottom: 20px;
            color: var(--text);
        }
        .bar-chart {
            display: flex;
            align-items: flex-end;
            gap: 12px;
            height: 200px;
            padding: 0 8px;
        }
        .bar-item {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
        }
        .bar {
            width: 100%;
            max-width: 60px;
            background: linear-gradient(180deg, var(--primary-light), var(--primary));
            border-radius: 6px 6px 0 0;
            min-height: 4px;
            transition: height 0.5s ease;
            position: relative;
        }
        .bar-label {
            font-size: 11px;
            color: var(--text-secondary);
            white-space: nowrap;
        }
        .bar-value {
            font-size: 11px;
            color: var(--text);
            font-weight: 600;
        }

        /* ===== 数据表格 ===== */
        .data-table-wrapper {
            background: var(--card);
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            border: 1px solid var(--border);
            overflow: hidden;
        }
        .data-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        .data-table thead {
            background: #f1f5f9;
        }
        .data-table th {
            text-align: left;
            padding: 12px 16px;
            font-weight: 600;
            color: var(--text-secondary);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            white-space: nowrap;
        }
        .data-table td {
            padding: 12px 16px;
            border-top: 1px solid var(--border);
            color: var(--text);
        }
        .data-table tr:hover td {
            background: #f8fafc;
        }
        .badge {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        .badge-success { background: #dcfce7; color: #166534; }
        .badge-warning { background: #fef3c7; color: #92400e; }
        .badge-danger { background: #fee2e2; color: #991b1b; }
        .btn-mini {
            padding: 3px 10px; margin: 1px 2px; font-size: 12px;
            border: 1px solid var(--border-color); border-radius: 6px;
            background: #fff; color: var(--text-primary); cursor: pointer;
            transition: all .15s;
        }
        .btn-mini:hover { border-color: var(--primary-color); color: var(--primary-color); background: var(--bg-primary); }
        .btn-mini-danger:hover { border-color: #dc2626; color: #dc2626; background: #fef2f2; }
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: var(--text-secondary);
        }
        .empty-state .icon { font-size: 48px; margin-bottom: 16px; }

        /* 日期筛选 */
        .filter-bar {
            display: flex;
            gap: 12px;
            align-items: center;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .filter-bar input[type="date"] {
            padding: 8px 12px;
            border: 1px solid var(--border);
            border-radius: 8px;
            font-size: 13px;
            outline: none;
        }
        .filter-bar input[type="date"]:focus {
            border-color: var(--primary);
        }
        .filter-btn {
            padding: 8px 20px;
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
        }
        .filter-btn:hover { background: var(--primary-dark); }

        /* ===== 聊天界面 ===== */
        .chat-layout {
            display: flex;
            flex-direction: column;
            height: 100%;
        }
        .chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
        }
        .chat-msg {
            display: flex;
            margin-bottom: 16px;
            animation: fadeIn 0.3s ease;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .chat-msg.user { justify-content: flex-end; }
        .chat-msg.bot { justify-content: flex-start; }
        .chat-avatar {
            width: 36px; height: 36px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
            flex-shrink: 0;
        }
        .chat-msg.user .chat-avatar { background: var(--primary-light); color: white; margin-left: 10px; order: 2; }
        .chat-msg.bot .chat-avatar { background: #e2e8f0; margin-right: 10px; }
        .chat-bubble {
            max-width: 70%;
            padding: 12px 16px;
            border-radius: 16px;
            font-size: 14px;
            line-height: 1.6;
            word-wrap: break-word;
            white-space: pre-wrap;
        }
        .chat-msg.user .chat-bubble {
            background: var(--primary);
            color: white;
            border-bottom-right-radius: 4px;
        }
        .chat-msg.bot .chat-bubble {
            background: var(--card);
            border: 1px solid var(--border);
            border-bottom-left-radius: 4px;
        }
        .chat-input-area {
            padding: 16px 20px;
            background: var(--card);
            border-top: 1px solid var(--border);
            display: flex;
            gap: 12px;
        }
        .chat-input-area textarea {
            flex: 1;
            padding: 12px 16px;
            border: 1px solid var(--border);
            border-radius: 12px;
            font-size: 14px;
            resize: none;
            outline: none;
            font-family: inherit;
            min-height: 44px;
            max-height: 120px;
        }
        .chat-input-area textarea:focus { border-color: var(--primary); }
        .chat-send-btn {
            padding: 0 24px;
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 12px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
            white-space: nowrap;
        }
        .chat-send-btn:hover { background: var(--primary-dark); }
        .chat-send-btn:disabled { background: #cbd5e1; cursor: not-allowed; }
        .typing-dots { display: flex; gap: 4px; padding: 4px 0; }
        .typing-dots span {
            width: 7px; height: 7px;
            background: #94a3b8;
            border-radius: 50%;
            animation: bounce 1.4s infinite;
        }
        .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
        .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes bounce {
            0%, 60%, 100% { transform: translateY(0); }
            30% { transform: translateY(-6px); }
        }

        /* 页面切换 */
        .page { display: none; height: 100%; }
        .page.active { display: flex; flex-direction: column; }

        /* 加载状态 */
        .loading {
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
        }
        .spinner {
            display: inline-block;
            width: 32px; height: 32px;
            border: 3px solid var(--border);
            border-top-color: var(--primary);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* 响应式 */
        @media (max-width: 768px) {
            .sidebar { width: 60px; }
            .sidebar-header h2, .sidebar-header small, .nav-item span:not(.icon), .sidebar-footer { display: none; }
            .nav-item { justify-content: center; padding: 14px; }
            .nav-item .icon { width: auto; }
            .content-body { padding: 16px; }
            .stats-grid { grid-template-columns: 1fr 1fr; gap: 12px; }
        }
    </style>
</head>
<body>
    <!-- 登录页 -->
    <div class="login-page" id="loginPage">
        <div class="login-card">
            <h1>✂️ 美业AI店长助手</h1>
            <p>说人话，管门店 — 让开店更轻松</p>
            <div class="brand-sub">价格透明 · 零推销 · 售后有保障</div>
            <div class="form-group">
                <label>用户名</label>
                <input type="text" id="loginUser" placeholder="请输入用户名" autocomplete="username">
            </div>
            <div class="form-group">
                <label>密码</label>
                <input type="password" id="loginPass" placeholder="请输入密码" autocomplete="current-password"
                    onkeydown="if(event.key==='Enter') doLogin()">
            </div>
            <button class="login-btn" onclick="doLogin()">登 录</button>
            <div class="login-error" id="loginError"></div>
        </div>
    </div>

    <!-- 主应用 -->
    <div class="app-layout" id="appLayout">
        <!-- 侧边栏 -->
        <div class="sidebar">
            <div class="sidebar-header">
                <h2>✂️ 美业助手</h2>
                <small>AI 店长管家</small>
            </div>
            <div class="sidebar-nav">
                <div class="nav-item active" onclick="switchPage('dashboard')" data-page="dashboard">
                    <span class="icon">📊</span><span>经营看板</span>
                </div>
                <div class="nav-item" onclick="switchPage('chat')" data-page="chat">
                    <span class="icon">💬</span><span>AI 店长</span>
                </div>
                <div class="nav-item" onclick="switchPage('employees')" data-page="employees">
                    <span class="icon">👩‍🎨</span><span>技师管理</span>
                </div>
                <div class="nav-item" onclick="switchPage('customers')" data-page="customers">
                    <span class="icon">🧑‍🤝‍🧑</span><span>顾客档案</span>
                </div>
                <div class="nav-item" onclick="switchPage('services')" data-page="services">
                    <span class="icon">💇</span><span>服务记录</span>
                </div>
                <div class="nav-item" onclick="switchPage('sales')" data-page="sales">
                    <span class="icon">🛍️</span><span>产品销售</span>
                </div>
                <div class="nav-item" onclick="switchPage('memberships')" data-page="memberships">
                    <span class="icon">💎</span><span>会员管理</span>
                </div>
                <div class="nav-item" onclick="switchPage('products')" data-page="products">
                    <span class="icon">📦</span><span>库存管理</span>
                </div>
                <div class="nav-item" onclick="switchPage('knowledge')" data-page="knowledge">
                    <span class="icon">📚</span><span>知识库</span>
                </div>
                <div class="nav-item" onclick="switchPage('appointments')" data-page="appointments">
                    <span class="icon">📅</span><span>预约管理</span>
                </div>
                <div class="nav-item" onclick="switchPage('msgstream')" data-page="msgstream">
                    <span class="icon">🗨️</span><span>顾客消息流</span>
                </div>
            </div>
            <div class="sidebar-footer">
                <button class="logout-btn" onclick="doLogout()">退出登录</button>
            </div>
        </div>

        <!-- 主内容 -->
        <div class="main-content">
            <!-- 仪表盘页 -->
            <div class="page active" id="page-dashboard">
                <div class="content-header">
                    <h1>📊 经营看板</h1>
                    <span id="dashboardDate" style="color: var(--text-secondary); font-size: 14px;"></span>
                    <span id="backupStatus" style="color: var(--text-secondary); font-size: 13px; margin-left: auto;"></span>
                </div>
                <div class="content-body">
                    <div id="onboardingBar" style="display:none; background: linear-gradient(135deg, #fff1f4, #ffe4ec); border: 1px solid var(--primary-light, #fce7f3); border-radius: 12px; padding: 14px 18px; margin-bottom: 16px;">
                        <strong>👋 3步上手：</strong>
                        ① 去 <a href="javascript:void(0)" onclick="switchPage('chat')">AI店长</a> 说「帮我记一笔张姐剪发38块」
                        → ② 回到这里看营收
                        → ③ 在 <a href="javascript:void(0)" onclick="switchPage('memberships')">会员管理</a> 给熟客开张储值卡
                    </div>
                    <div class="stats-grid">
                        <div class="stat-card">
                            <div class="label">💰 今日营收</div>
                            <div class="value">¥<span id="todayRevenue">0</span></div>
                        </div>
                        <div class="stat-card">
                            <div class="label">💇 今日服务</div>
                            <div class="value"><span id="todayCount">0</span><span class="unit">单</span></div>
                        </div>
                        <div class="stat-card">
                            <div class="label">👩‍🎨 在岗技师</div>
                            <div class="value"><span id="staffCount">0</span><span class="unit">人</span></div>
                        </div>
                    </div>
                    <div class="chart-card" style="border: 1px solid #fce7f3; background: linear-gradient(135deg, #fff, #fdf2f8);">
                        <h3>✨ AI店长 <span id="contribMonth"></span> 功劳簿</h3>
                        <div id="contributions" style="display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; padding: 6px 0;">
                            <div class="contrib-item">📝 本月帮你记录 <strong id="cbRecords">-</strong> 笔经营流水（约省 <strong id="cbHours">-</strong> 小时记账时间）</div>
                            <div class="contrib-item">💎 新增会员 <strong id="cbMembers">-</strong> 位</div>
                            <div class="contrib-item">⏰ 盯着 <strong id="cbExpiring">-</strong> 张卡30天内到期，涉及余额 <strong id="cbBalance">-</strong> 元，记得让AI店长提醒召回</div>
                            <div class="contrib-item">📦 库存预警 <strong id="cbStock">-</strong> 项，避免断货</div>
                        </div>
                    </div>
                    <div class="chart-card">
                        <h3>📈 近7天营收趋势</h3>
                        <div class="bar-chart" id="weeklyChart"></div>
                    </div>
                </div>
            </div>

            <!-- 聊天页 -->
            <div class="page" id="page-chat">
                <div class="chat-layout">
                    <div class="content-header">
                        <h1>💬 AI 店长</h1>
                    </div>
                    <div class="chat-messages" id="chatMessages">
                        <div class="chat-msg bot">
                            <div class="chat-avatar">🤖</div>
                            <div class="chat-bubble">你好！我是你的美业AI店长助手 ✂️ 你可以问我：今天营业额多少？阿杰做了几个客人？帮我记一笔李姐烫发198块。也可以查会员余额、管库存、看技师提成。说人话就行！</div>
                        </div>
                    </div>
                    <div class="chat-input-area">
                        <textarea id="chatInput" placeholder="说人话就行，比如：今天赚了多少？帮我记一笔..." rows="1"
                            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat();}"
                            oninput="this.style.height='auto';this.style.height=Math.min(this.scrollHeight,120)+'px'"></textarea>
                        <button class="chat-send-btn" id="chatSendBtn" onclick="sendChat()">发送</button>
                    </div>
                </div>
            </div>

            <!-- 员工页 -->
            <div class="page" id="page-employees">
                <div class="content-header"><h1>👩‍🎨 技师管理</h1></div>
                <div class="content-body">
                    <div class="data-table-wrapper">
                        <table class="data-table">
                            <thead><tr>
                                <th>ID</th><th>姓名</th><th>岗位</th><th>提成率</th><th>状态</th>
                            </tr></thead>
                            <tbody id="employeesBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- 顾客页 -->
            <div class="page" id="page-customers">
                <div class="content-header"><h1>🧑‍🤝‍🧑 顾客档案</h1></div>
                <div class="content-body">
                    <div style="display:flex; gap:10px; margin-bottom:14px;">
                        <input id="customerSearch" oninput="renderCustomerTable()" placeholder="🔍 搜索姓名/电话/备注..." style="flex:0 0 260px; padding:8px 14px; border:1px solid var(--border-color); border-radius:8px; font-size:14px;">
                    </div>
                    <div class="data-table-wrapper">
                        <table class="data-table">
                            <thead><tr>
                                <th>ID</th><th>姓名</th><th>电话</th><th>备注</th><th>创建时间</th>
                            </tr></thead>
                            <tbody id="customersBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- 服务记录页 -->
            <div class="page" id="page-services">
                <div class="content-header"><h1>💇 服务记录</h1></div>
                <div class="content-body">
                    <div class="filter-bar">
                        <label>起始日期:</label>
                        <input type="date" id="svcStartDate">
                        <label>结束日期:</label>
                        <input type="date" id="svcEndDate">
                        <button class="filter-btn" onclick="loadServices()">查询</button>
                    </div>
                    <div class="data-table-wrapper">
                        <table class="data-table">
                            <thead><tr>
                                <th>ID</th><th>日期</th><th>顾客</th><th>员工</th><th>服务类型</th>
                                <th>金额</th><th>提成</th><th>净收入</th><th>状态</th><th>备注</th>
                            </tr></thead>
                            <tbody id="servicesBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- 商品销售页 -->
            <div class="page" id="page-sales">
                <div class="content-header"><h1>🛍️ 产品销售</h1></div>
                <div class="content-body">
                    <div class="filter-bar">
                        <label>起始日期:</label>
                        <input type="date" id="saleStartDate">
                        <label>结束日期:</label>
                        <input type="date" id="saleEndDate">
                        <button class="filter-btn" onclick="loadSales()">查询</button>
                    </div>
                    <div class="data-table-wrapper">
                        <table class="data-table">
                            <thead><tr>
                                <th>ID</th><th>日期</th><th>商品</th><th>顾客</th>
                                <th>数量</th><th>单价</th><th>总金额</th><th>状态</th><th>备注</th>
                            </tr></thead>
                            <tbody id="salesBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- 会员卡页 -->
            <div class="page" id="page-memberships">
                <div class="content-header"><h1>💎 会员管理</h1></div>
                <div class="content-body">
                    <div id="memberAlertBar" style="display:none; background:#fff1f4; border:1px solid #fecdd3; border-radius:12px; padding:12px 18px; margin-bottom:14px; color:#9f1239;"></div>
                    <div style="display:flex; gap:10px; margin-bottom:14px;">
                        <input id="memberSearch" oninput="renderMemberTable()" placeholder="🔍 搜索顾客姓名/卡类型..." style="flex:0 0 260px; padding:8px 14px; border:1px solid var(--border-color); border-radius:8px; font-size:14px;">
                        <span style="align-self:center; color:var(--text-secondary); font-size:13px;">操作按钮会把指令填到AI店长，你确认后执行</span>
                    </div>
                    <div class="data-table-wrapper">
                        <table class="data-table">
                            <thead><tr>
                                <th>ID</th><th>顾客</th><th>卡类型</th><th>总金额</th>
                                <th>余额</th><th>剩余次数</th><th>积分</th><th>到期日</th><th>状态</th><th>操作</th>
                            </tr></thead>
                            <tbody id="membershipsBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- 商品库存页 -->
            <div class="page" id="page-products">
                <div class="content-header"><h1>📦 库存管理</h1></div>
                <div class="content-body">
                    <div class="data-table-wrapper">
                        <table class="data-table">
                            <thead><tr>
                                <th>ID</th><th>名称</th><th>类别</th><th>单价</th><th>库存</th><th>低库存阈值</th>
                            </tr></thead>
                            <tbody id="productsBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- 知识库管理页 -->
                        <!-- 预约管理页 -->
            <div class="page" id="page-appointments">
                <div class="content-header">
                    <h1>📅 预约管理</h1>
                    <p>AI 记下的每个预约都在这里，不再靠脑子记</p>
                </div>
                <div class="card">
                    <div style="display:flex;gap:12px;align-items:center;margin-bottom:12px;flex-wrap:wrap;">
                        <label>日期 <input type="date" id="apptDate" onchange="loadAppointments()" style="padding:6px 10px;border:1px solid var(--border);border-radius:8px;"></label>
                        <label>状态
                            <select id="apptStatus" onchange="loadAppointments()" style="padding:6px 10px;border:1px solid var(--border);border-radius:8px;">
                                <option value="">全部</option>
                                <option value="pending">待确认</option>
                                <option value="confirmed">已确认</option>
                                <option value="completed">已完成</option>
                                <option value="cancelled">已取消</option>
                                <option value="no_show">爽约</option>
                            </select>
                        </label>
                        <button onclick="document.getElementById('apptDate').value='';loadAppointments()" style="padding:6px 12px;border:1px solid var(--border);background:#fff;border-radius:8px;cursor:pointer;">查看未来30天</button>
                    </div>
                    <div id="apptTable"></div>
                </div>
            </div>

            <!-- 顾客消息流页 -->
            <div class="page" id="page-msgstream">
                <div class="content-header">
                    <h1>🗨️ 顾客消息流</h1>
                    <p>顾客和 AI 的对话全程可见，需要你处理的会标红</p>
                </div>
                <div style="display:grid;grid-template-columns:280px 1fr;gap:16px;" id="msgStreamLayout">
                    <div class="card" style="align-self:start;">
                        <div id="sessionList"></div>
                    </div>
                    <div class="card">
                        <div id="chatStream"><p style="color:var(--text-tertiary);">← 从左侧选择一个会话查看对话</p></div>
                    </div>
                </div>
            </div>

            <div class="page" id="page-knowledge">
                <div class="content-header">
                    <h1>📚 知识库管理</h1>
                    <div style="display:flex;gap:8px;align-items:center;">
                        <select id="kbCategory" onchange="loadKnowledge()" style="padding:6px 12px;border-radius:6px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary);">
                            <option value="">全部分类</option>
                            <option value="tuning">🔊 调频规则</option>
                            <option value="faq">❓ FAQ</option>
                            <option value="sop">📋 SOP</option>
                            <option value="industry">💡 行业常识</option>
                        </select>
                        <input id="kbSearch" placeholder="搜索关键词..." onkeydown="if(event.key==='Enter')loadKnowledge()" style="padding:6px 12px;border-radius:6px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary);width:200px;">
                        <button onclick="loadKnowledge()" style="padding:6px 14px;border-radius:6px;background:var(--accent);color:#fff;border:none;cursor:pointer;">搜索</button>
                        <button onclick="showKbAddForm()" style="padding:6px 14px;border-radius:6px;background:#10b981;color:#fff;border:none;cursor:pointer;">+ 新增</button>
                    </div>
                </div>
                <div class="content-body">
                    <div id="kbStats" style="display:flex;gap:16px;margin-bottom:16px;"></div>
                    <div id="kbAddForm" style="display:none;margin-bottom:16px;padding:16px;background:var(--bg-secondary);border-radius:8px;border:1px solid var(--border-color);">
                        <h3 style="margin:0 0 12px;" id="kbAddFormTitle">新增知识库条目</h3>
                        <div style="display:grid;grid-template-columns:120px 1fr;gap:8px;align-items:center;">
                            <label>分类：</label>
                            <select id="kbAddCategory" style="padding:6px;border-radius:4px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary);">
                                <option value="faq">FAQ</option><option value="tuning">调频规则</option><option value="sop">SOP</option><option value="industry">行业常识</option>
                            </select>
                            <label>问题：</label>
                            <input id="kbAddQuestion" placeholder="如：你们几点开门" style="padding:6px;border-radius:4px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary);">
                            <label>关键词：</label>
                            <input id="kbAddKeywords" placeholder="逗号分隔，如：营业时间,几点开门" style="padding:6px;border-radius:4px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary);">
                            <label>回答：</label>
                            <textarea id="kbAddAnswer" rows="3" placeholder="标准回答内容" style="padding:6px;border-radius:4px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary);"></textarea>
                            <label>追问：</label>
                            <input id="kbAddFollowup" placeholder="可选，推荐追问" style="padding:6px;border-radius:4px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary);">
                        </div>
                        <div style="margin-top:12px;display:flex;gap:8px;">
                            <button onclick="doKbAdd()" style="padding:6px 16px;border-radius:6px;background:#10b981;color:#fff;border:none;cursor:pointer;">确认新增</button>
                            <button onclick="document.getElementById('kbAddForm').style.display='none'" style="padding:6px 16px;border-radius:6px;background:var(--bg-primary);color:var(--text-primary);border:1px solid var(--border-color);cursor:pointer;">取消</button>
                        </div>
                    </div>
                    <div class="data-table-wrapper">
                        <table class="data-table">
                            <thead><tr>
                                <th style="width:40px">#</th><th style="width:60px">分类</th><th>问题</th><th>关键词</th><th>回答</th><th style="width:100px">操作</th>
                            </tr></thead>
                            <tbody id="kbBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

<script>
// ==================== 全局状态 ====================
let token = localStorage.getItem('auth_token') || '';
const sessionId = 'web_' + Math.random().toString(36).substr(2, 9);

// ==================== 工具函数 ====================
function esc(text) {
    if (!text) return '';
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}

async function api(path, options = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const resp = await fetch(path, { ...options, headers });
    if (resp.status === 401) {
        token = '';
        localStorage.removeItem('auth_token');
        showLogin();
        throw new Error('未授权');
    }
    return resp;
}

// ==================== 预约管理 ====================
const APPT_STATUS_LABEL = {pending:'待确认', confirmed:'已确认', completed:'已完成', cancelled:'已取消', no_show:'爽约'};

async function loadAppointments() {
    try {
        const date = document.getElementById('apptDate').value || '';
        const status = document.getElementById('apptStatus').value || '';
        const resp = await api(`/api/appointments?date=${encodeURIComponent(date)}&status=${encodeURIComponent(status)}`);
        const j = await resp.json();
        const rows = j.data || [];
        if (!rows.length) {
            document.getElementById('apptTable').innerHTML = '<p style="color:var(--text-tertiary);">暂无预约。顾客跟 AI 说"明天下午两点剪发"，AI 会自动记到这里。</p>';
            return;
        }
        let html = '<table style="width:100%;border-collapse:collapse;"><tr style="text-align:left;color:var(--text-tertiary);font-size:13px;"><th style="padding:8px;">顾客</th><th style="padding:8px;">项目</th><th style="padding:8px;">日期</th><th style="padding:8px;">时间</th><th style="padding:8px;">状态</th><th style="padding:8px;">操作</th></tr>';
        rows.forEach(r => {
            const badge = r.status === 'pending' ? 'background:#fff7e6;color:#d46b08;'
                : r.status === 'confirmed' ? 'background:#e6f7ff;color:#096dd9;'
                : r.status === 'completed' ? 'background:#f6ffed;color:#389e0d;'
                : 'background:#fff1f0;color:#cf1322;';
            html += `<tr style="border-top:1px solid var(--border);"><td style="padding:10px 8px;">${esc(r.customer_name)}</td><td style="padding:10px 8px;">${esc(r.service_name)}</td><td style="padding:10px 8px;">${esc(r.date)}</td><td style="padding:10px 8px;">${esc(r.time)}</td><td style="padding:10px 8px;"><span style="padding:2px 10px;border-radius:10px;font-size:12px;${badge}">${APPT_STATUS_LABEL[r.status]||r.status}</span></td><td style="padding:10px 8px;">`;
            if (r.status === 'pending') {
                html += `<button onclick="setApptStatus(${r.id},'confirmed')" style="padding:4px 10px;margin-right:6px;cursor:pointer;border:1px solid #91caff;background:#e6f7ff;border-radius:6px;">确认</button>`;
            }
            if (r.status === 'pending' || r.status === 'confirmed') {
                html += `<button onclick="setApptStatus(${r.id},'completed')" style="padding:4px 10px;margin-right:6px;cursor:pointer;border:1px solid #b7eb8f;background:#f6ffed;border-radius:6px;">完成</button>`;
            }
            html += `</td></tr>`;
        });
        html += '</table>';
        document.getElementById('apptTable').innerHTML = html;
    } catch (e) {
        document.getElementById('apptTable').innerHTML = `<p style="color:#cf1322;">加载失败：${esc(String(e))}</p>`;
    }
}

async function setApptStatus(id, status) {
    try {
        await api(`/api/appointments/${id}/status`, { method: 'PUT', body: JSON.stringify({ status }) });
        loadAppointments();
    } catch (e) { alert('操作失败：' + e); }
}

// ==================== 顾客消息流 ====================
let currentSessionId = '';

async function loadChatSessions() {
    try {
        const resp = await api('/api/chat_sessions');
        const j = await resp.json();
        const sessions = j.data || [];
        const el = document.getElementById('sessionList');
        if (!sessions.length) {
            el.innerHTML = '<p style="color:var(--text-tertiary);">暂无对话。顾客在微信上发给 AI 的每条消息，都会出现在这里。</p>';
            document.getElementById('chatStream').innerHTML = '';
            return;
        }
        let html = sessions.map(s => {
            const flag = s.needs_human_count > 0 ? '<span style="color:#cf1322;font-size:12px;">🔴 需人工×' + s.needs_human_count + '</span>' : '';
            return `<div onclick="openSession('${esc(s.session_id)}')" style="padding:10px;border-bottom:1px solid var(--border);cursor:pointer;" onmouseover="this.style.background='var(--bg-hover)'" onmouseout="this.style.background=''">
                <div style="display:flex;justify-content:space-between;"><b>${esc(s.sender_name || s.session_id)}</b><span style="color:var(--text-tertiary);font-size:12px;">${s.message_count}条</span></div>
                <div style="color:var(--text-tertiary);font-size:13px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${s.last_direction==='out'?'AI: ':''}${esc(s.last_content)}</div>
                <div style="margin-top:4px;">${flag}</div>
            </div>`;
        }).join('');
        el.innerHTML = html;
    } catch (e) {
        document.getElementById('sessionList').innerHTML = `<p style="color:#cf1322;">加载失败：${esc(String(e))}</p>`;
    }
}

async function openSession(sid) {
    currentSessionId = sid;
    try {
        const resp = await api(`/api/chat_messages?session_id=${encodeURIComponent(sid)}&limit=100`);
        const j = await resp.json();
        const msgs = j.data || [];
        const el = document.getElementById('chatStream');
        if (!msgs.length) { el.innerHTML = '<p style="color:var(--text-tertiary);">该会话暂无消息</p>'; return; }
        el.innerHTML = msgs.map(m => {
            if (m.direction === 'in') {
                return `<div style="margin-bottom:10px;"><span style="background:#f0f2f5;padding:8px 12px;border-radius:10px;display:inline-block;max-width:80%;">${esc(m.content)}</span><div style="color:var(--text-tertiary);font-size:11px;margin-top:2px;">${esc(m.sender_name||'顾客')} · ${esc(m.created_at)}</div></div>`;
            }
            const warn = m.needs_human ? '<div style="color:#cf1322;font-size:12px;margin-top:2px;">🔴 需要人工介入</div>' : '';
            return `<div style="margin-bottom:10px;text-align:right;"><span style="background:#e6f7ff;padding:8px 12px;border-radius:10px;display:inline-block;max-width:80%;text-align:left;">${esc(m.content)}</span><div style="color:var(--text-tertiary);font-size:11px;margin-top:2px;">AI助手 · ${esc(m.created_at)}</div>${warn}</div>`;
        }).join('');
    } catch (e) {
        document.getElementById('chatStream').innerHTML = `<p style="color:#cf1322;">加载失败：${esc(String(e))}</p>`;
    }
}

// ==================== 登录 ====================
function showLogin() {
    document.getElementById('loginPage').style.display = 'flex';
    document.getElementById('appLayout').classList.remove('active');
}

function showApp() {
    document.getElementById('loginPage').style.display = 'none';
    document.getElementById('appLayout').classList.add('active');
    loadDashboard();
}

async function doLogin() {
    const user = document.getElementById('loginUser').value.trim();
    const pass = document.getElementById('loginPass').value;
    const errEl = document.getElementById('loginError');
    errEl.textContent = '';

    if (!user || !pass) { errEl.textContent = '请输入用户名和密码'; return; }

    try {
        const resp = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: user, password: pass })
        });
        const data = await resp.json();
        if (data.success) {
            token = data.token;
            localStorage.setItem('auth_token', token);
            showApp();
        } else {
            errEl.textContent = data.error || '登录失败';
        }
    } catch (e) {
        errEl.textContent = '网络错误，请重试';
    }
}

function doLogout() {
    token = '';
    localStorage.removeItem('auth_token');
    showLogin();
}

// ==================== 页面切换 ====================
function switchPage(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById('page-' + page).classList.add('active');
    document.querySelector(`.nav-item[data-page="${page}"]`).classList.add('active');

    // 加载数据
    const loaders = {
        dashboard: loadDashboard,
        employees: loadEmployees,
        customers: loadCustomers,
        services: loadServices,
        sales: loadSales,
        memberships: loadMemberships,
        products: loadProducts,
        knowledge: loadKnowledge,
        appointments: loadAppointments,
        msgstream: loadChatSessions,
    };
    if (loaders[page]) loaders[page]();
}

// ==================== 仪表盘 ====================
async function loadDashboard() {
    document.getElementById('dashboardDate').textContent = new Date().toLocaleDateString('zh-CN', {
        year: 'numeric', month: 'long', day: 'numeric', weekday: 'long'
    });

    // 数据备份状态（每日自动备份，让店主安心）
    try {
        const bResp = await api('/api/dashboard');
        const bData = await bResp.json();
        const el = document.getElementById('backupStatus');
        if (el && bData.backup) {
            el.textContent = `💾 数据已备份 ${bData.backup.time}`;
        }
    } catch(e) {}

    // 新手引导：首次登录显示3步上手（localStorage 记录，只显示前3次）
    try {
        const seen = parseInt(localStorage.getItem('onboarding_seen') || '0');
        if (seen < 3) {
            document.getElementById('onboardingBar').style.display = 'block';
            localStorage.setItem('onboarding_seen', String(seen + 1));
        }
    } catch(e) {}

    // AI功劳簿
    try {
        const cResp = await api('/api/ai_contributions');
        const c = await cResp.json();
        if (!c.error) {
            document.getElementById('contribMonth').textContent = c.month || '';
            document.getElementById('cbRecords').textContent = c.month_records || 0;
            document.getElementById('cbHours').textContent = (c.minutes_saved / 60).toFixed(1);
            document.getElementById('cbMembers').textContent = c.new_members || 0;
            document.getElementById('cbExpiring').textContent = c.expiring_soon || 0;
            document.getElementById('cbBalance').textContent = (c.expiring_balance || 0).toLocaleString();
            document.getElementById('cbStock').textContent = c.low_stock || 0;
        }
    } catch(e) { console.error(e); }

    try {
        const resp = await api('/api/dashboard');
        const data = await resp.json();
        if (data.error) return;

        document.getElementById('todayRevenue').textContent = (data.today_revenue || 0).toLocaleString();
        document.getElementById('todayCount').textContent = data.today_count || 0;
        document.getElementById('staffCount').textContent = data.staff_count || 0;

        // 绘制柱状图
        const chart = document.getElementById('weeklyChart');
        chart.innerHTML = '';
        const weekly = data.weekly_data || [];
        const maxRev = Math.max(...weekly.map(d => d.revenue), 1);

        weekly.forEach(d => {
            const pct = (d.revenue / maxRev) * 160;
            const dateStr = d.date.slice(5); // MM-DD
            chart.innerHTML += `
                <div class="bar-item">
                    <div class="bar-value">¥${d.revenue.toLocaleString()}</div>
                    <div class="bar" style="height: ${Math.max(pct, 4)}px"></div>
                    <div class="bar-label">${dateStr}</div>
                </div>
            `;
        });
    } catch (e) {
        console.error('Dashboard error:', e);
    }
}

// ==================== 聊天 ====================
async function sendChat() {
    const input = document.getElementById('chatInput');
    const btn = document.getElementById('chatSendBtn');
    const content = input.value.trim();
    if (!content) return;

    input.value = '';
    input.style.height = 'auto';
    btn.disabled = true;

    addChatMsg(content, true);
    addTyping();

    try {
        const resp = await api('/api/chat', {
            method: 'POST',
            body: JSON.stringify({ content, session_id: sessionId })
        });
        removeTyping();
        const data = await resp.json();
        addChatMsg(data.reply || data.error || '(无回复)', false);
    } catch (e) {
        removeTyping();
        if (e.message !== '未授权') addChatMsg('网络错误，请重试', false);
    } finally {
        btn.disabled = false;
        input.focus();
    }
}

function addChatMsg(text, isUser) {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'chat-msg ' + (isUser ? 'user' : 'bot');
    div.innerHTML = `
        <div class="chat-avatar">${isUser ? '👤' : '🤖'}</div>
        <div class="chat-bubble">${esc(text)}</div>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function addTyping() {
    const container = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'chat-msg bot';
    div.id = 'typingIndicator';
    div.innerHTML = `<div class="chat-avatar">🤖</div><div class="chat-bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function removeTyping() {
    const el = document.getElementById('typingIndicator');
    if (el) el.remove();
}

// ==================== 数据加载 ====================
function renderEmpty(tbodyId, cols) {
    document.getElementById(tbodyId).innerHTML = `<tr><td colspan="${cols}" class="empty-state"><div class="icon">📭</div>暂无数据</td></tr>`;
}

async function loadEmployees() {
    try {
        const resp = await api('/api/employees');
        const { data } = await resp.json();
        const tbody = document.getElementById('employeesBody');
        if (!data || !data.length) { renderEmpty('employeesBody', 5); return; }
        tbody.innerHTML = data.map(e => `<tr>
            <td>${e.id}</td>
            <td><strong>${esc(e.name)}</strong></td>
            <td>${e.role === 'manager' ? '管理员' : e.role === 'bot' ? '机器人' : '员工'}</td>
            <td>${e.commission_rate}%</td>
            <td>${e.is_active ? '<span class="badge badge-success">在职</span>' : '<span class="badge badge-danger">离职</span>'}</td>
        </tr>`).join('');
    } catch (e) { console.error(e); }
}

let _customerData = [];

async function loadCustomers() {
    try {
        const resp = await api('/api/customers');
        const { data } = await resp.json();
        _customerData = data || [];
        renderCustomerTable();
    } catch (e) { console.error(e); }
}

function renderCustomerTable() {
    const tbody = document.getElementById('customersBody');
    if (!_customerData.length) { renderEmpty('customersBody', 5); return; }
    const kw = (document.getElementById('customerSearch')?.value || '').trim().toLowerCase();
    const rows = kw
        ? _customerData.filter(c => (c.name||'').toLowerCase().includes(kw) || (c.phone||'').includes(kw) || (c.notes||'').toLowerCase().includes(kw))
        : _customerData;
    if (!rows.length) { tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text-secondary);padding:24px;">没有匹配的顾客</td></tr>`; return; }
    tbody.innerHTML = rows.map(c => `<tr>
        <td>${c.id}</td>
        <td><strong>${esc(c.name)}</strong></td>
        <td>${esc(c.phone || '-')}</td>
        <td>${esc(c.notes || '-')}</td>
        <td>${c.created_at ? c.created_at.slice(0,10) : '-'}</td>
    </tr>`).join('');
}

async function loadServices() {
    try {
        const start = document.getElementById('svcStartDate').value;
        const end = document.getElementById('svcEndDate').value;
        let url = '/api/services';
        const params = [];
        if (start) params.push('start_date=' + start);
        if (end) params.push('end_date=' + end);
        if (params.length) url += '?' + params.join('&');

        const resp = await api(url);
        const { data } = await resp.json();
        const tbody = document.getElementById('servicesBody');
        if (!data || !data.length) { renderEmpty('servicesBody', 10); return; }
        tbody.innerHTML = data.map(r => `<tr>
            <td>${r.id}</td>
            <td>${r.service_date || '-'}</td>
            <td>${esc(r.customer_name || '-')}</td>
            <td>${esc(r.employee_name || '-')}</td>
            <td>${esc(r.service_type || '-')}</td>
            <td>¥${(r.amount || 0).toLocaleString()}</td>
            <td>¥${(r.commission_amount || 0).toLocaleString()}</td>
            <td>${r.net_amount != null ? '¥' + r.net_amount.toLocaleString() : '-'}</td>
            <td>${r.confirmed ? '<span class="badge badge-success">已确认</span>' : '<span class="badge badge-warning">待确认</span>'}</td>
            <td>${esc(r.notes || '-')}</td>
        </tr>`).join('');
    } catch (e) { console.error(e); }
}

async function loadSales() {
    try {
        const start = document.getElementById('saleStartDate').value;
        const end = document.getElementById('saleEndDate').value;
        let url = '/api/sales';
        const params = [];
        if (start) params.push('start_date=' + start);
        if (end) params.push('end_date=' + end);
        if (params.length) url += '?' + params.join('&');

        const resp = await api(url);
        const { data } = await resp.json();
        const tbody = document.getElementById('salesBody');
        if (!data || !data.length) { renderEmpty('salesBody', 9); return; }
        tbody.innerHTML = data.map(r => `<tr>
            <td>${r.id}</td>
            <td>${r.sale_date || '-'}</td>
            <td>${esc(r.product_name || '-')}</td>
            <td>${esc(r.customer_name || '-')}</td>
            <td>${r.quantity}</td>
            <td>${r.unit_price != null ? '¥' + r.unit_price.toLocaleString() : '-'}</td>
            <td>¥${(r.total_amount || 0).toLocaleString()}</td>
            <td>${r.confirmed ? '<span class="badge badge-success">已确认</span>' : '<span class="badge badge-warning">待确认</span>'}</td>
            <td>${esc(r.notes || '-')}</td>
        </tr>`).join('');
    } catch (e) { console.error(e); }
}

let _memberData = [];

async function loadMemberships() {
    try {
        const resp = await api('/api/memberships');
        const { data } = await resp.json();
        _memberData = data || [];
        renderMemberTable();
        renderMemberAlert();
    } catch (e) { console.error(e); }
}

function renderMemberTable() {
    const tbody = document.getElementById('membershipsBody');
    if (!_memberData.length) { renderEmpty('membershipsBody', 10); return; }
    const kw = (document.getElementById('memberSearch')?.value || '').trim().toLowerCase();
    const rows = kw
        ? _memberData.filter(m => (m.customer_name || '').toLowerCase().includes(kw) || (m.card_type || '').toLowerCase().includes(kw))
        : _memberData;
    if (!rows.length) { tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:var(--text-secondary);padding:24px;">没有匹配的会员</td></tr>`; return; }
    tbody.innerHTML = rows.map(m => {
        const isSessionCard = m.remaining_sessions != null;
        const ops = m.is_active ? `
            <button class="btn-mini" onclick="quickMemberOp(${m.id}, '${esc(m.customer_name||'')}', '${esc(m.card_type||'')}', '${isSessionCard ? 'redeem' : 'deduct'}')">${isSessionCard ? '扣次' : '扣款'}</button>
            <button class="btn-mini" onclick="quickMemberOp(${m.id}, '${esc(m.customer_name||'')}', '${esc(m.card_type||'')}', 'renew')">续卡</button>
            <button class="btn-mini btn-mini-danger" onclick="quickMemberOp(${m.id}, '${esc(m.customer_name||'')}', '${esc(m.card_type||'')}', 'refund')">退卡</button>` : '-';
        return `<tr>
            <td>${m.id}</td>
            <td><strong>${esc(m.customer_name || '-')}</strong></td>
            <td>${esc(m.card_type || '-')}</td>
            <td>¥${(m.total_amount || 0).toLocaleString()}</td>
            <td>¥${(m.balance || 0).toLocaleString()}</td>
            <td>${m.remaining_sessions != null ? m.remaining_sessions : '-'}</td>
            <td>${m.points || 0}</td>
            <td>${m.expires_at || '-'}</td>
            <td>${m.is_active ? '<span class="badge badge-success">有效</span>' : '<span class="badge badge-danger">已过期</span>'}</td>
            <td>${ops}</td>
        </tr>`;
    }).join('');
}

function renderMemberAlert() {
    const bar = document.getElementById('memberAlertBar');
    const today = new Date(); today.setHours(0,0,0,0);
    const soon = _memberData.filter(m => {
        if (!m.is_active || !m.expires_at) return false;
        const exp = new Date(m.expires_at + 'T00:00:00');
        const days = Math.round((exp - today) / 86400000);
        return days >= 0 && days <= 30;
    });
    if (soon.length) {
        const totalBal = soon.reduce((s,m) => s + (m.balance||0), 0);
        bar.style.display = 'block';
        bar.innerHTML = `⏰ <strong>${soon.length} 张会员卡将在30天内到期</strong>，涉及余额 ¥${totalBal.toLocaleString()}：${soon.slice(0,5).map(m => `${esc(m.customer_name)}(${m.expires_at.slice(5)})`).join('、')}${soon.length>5?' 等':''}。可在AI店长说「查一下快到期的会员卡」安排召回。`;
    } else { bar.style.display = 'none'; }
}

function quickMemberOp(id, name, cardType, op) {
    const cmdMap = {
        redeem: `帮${name}核销${cardType}卡（卡号${id}）扣1次`,
        deduct: `从${name}的${cardType}卡（卡号${id}）扣款`,
        renew: `给${name}的${cardType}卡（卡号${id}）续卡`,
        refund: `给${name}办理退卡（卡号${id}），请说明退款规则`,
    };
    const msg = cmdMap[op];
    switchPage('chat');
    const input = document.getElementById('chatInput');
    input.value = msg;
    input.focus();
}

async function loadProducts() {
    try {
        const resp = await api('/api/products');
        const { data } = await resp.json();
        const tbody = document.getElementById('productsBody');
        if (!data || !data.length) { renderEmpty('productsBody', 6); return; }
        tbody.innerHTML = data.map(p => `<tr>
            <td>${p.id}</td>
            <td><strong>${esc(p.name)}</strong></td>
            <td>${esc(p.category || '-')}</td>
            <td>${p.unit_price != null ? '¥' + p.unit_price.toLocaleString() : '-'}</td>
            <td>${p.stock_quantity <= p.low_stock_threshold ? '<span class="badge badge-danger">' + p.stock_quantity + '</span>' : p.stock_quantity}</td>
            <td>${p.low_stock_threshold}</td>
        </tr>`).join('');
    } catch (e) { console.error(e); }
}

// ==================== 知识库管理 ====================
const kbCategoryLabels = {tuning:'🔊调频', faq:'❓FAQ', sop:'📋SOP', industry:'💡常识'};

async function loadKnowledge() {
    try {
        // 加载统计
        const statsResp = await api('/api/knowledge_base');
        const statsData = await statsResp.json();
        if (statsData.stats) {
            const s = statsData.stats;
            document.getElementById('kbStats').innerHTML =
                Object.entries(kbCategoryLabels).map(([k, label]) =>
                    `<div style="padding:8px 16px;background:var(--bg-secondary);border-radius:8px;border:1px solid var(--border-color);"><div style="font-size:12px;color:var(--text-secondary);">${label}</div><div style="font-size:24px;font-weight:700;">${s[k]||0}</div></div>`
                ).join('') + `<div style="padding:8px 16px;background:var(--accent);border-radius:8px;color:#fff;"><div style="font-size:12px;">总计</div><div style="font-size:24px;font-weight:700;">${s.total||0}</div></div>`;
        }

        // 加载条目
        const category = document.getElementById('kbCategory').value;
        const searchQ = document.getElementById('kbSearch').value.trim();
        let items = [];
        if (searchQ) {
            const resp = await api('/api/knowledge_base/search?q=' + encodeURIComponent(searchQ));
            const data = await resp.json();
            items = data.results || [];
        } else {
            const resp = await api('/api/knowledge_base/items' + (category ? '?category=' + category : ''));
            const data = await resp.json();
            items = data.items || [];
        }

        const tbody = document.getElementById('kbBody');
        if (!items.length) { renderEmpty('kbBody', 6); return; }
        tbody.innerHTML = items.map((item, i) => `<tr>
            <td>${i+1}</td>
            <td><span style="font-size:12px;padding:2px 8px;border-radius:4px;background:var(--bg-secondary);">${kbCategoryLabels[item.category]||item.category}</span></td>
            <td><strong>${esc(item.question)}</strong></td>
            <td style="font-size:12px;color:var(--text-secondary);">${(item.keywords||[]).map(k=>'<span style="padding:1px 6px;margin:1px;border-radius:3px;background:var(--bg-secondary);">'+esc(k)+'</span>').join(' ')}</td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;">${esc(item.answer)}</td>
            <td style="white-space:nowrap;">
                <button onclick="showKbEditForm('${item.category}',${item.id})" class="btn-mini">编辑</button>
                <button onclick="doKbDelete('${item.category}',${item.id})" class="btn-mini btn-mini-danger">删除</button>
            </td>
        </tr>`).join('');
    } catch (e) { console.error(e); }
}

function showKbEditForm(category, id) {
    // 复用新增表单做编辑：找到条目数据填充
    const tr = event.target.closest('tr');
    const cells = tr.querySelectorAll('td');
    document.getElementById('kbAddForm').style.display = 'block';
    document.getElementById('kbAddCategory').value = category;
    document.getElementById('kbAddQuestion').value = cells[2].innerText.trim();
    document.getElementById('kbAddKeywords').value = cells[3].innerText.trim().split(/\s+/).join(',');
    document.getElementById('kbAddAnswer').value = cells[4].innerText.trim();
    document.getElementById('kbAddFollowup').value = '';
    document.getElementById('kbAddFormTitle').textContent = '✏️ 编辑条目';
    document.getElementById('kbAddForm').dataset.editCategory = category;
    document.getElementById('kbAddForm').dataset.editId = id;
}

async function doKbDelete(category, index) {
    if (!confirm('确定删除这条知识库条目吗？')) return;
    try {
        const resp = await api('/api/knowledge_base/items?category='+category+'&index='+index, {method:'DELETE'});
        const data = await resp.json();
        if (data.success) { loadKnowledge(); } else { alert('删除失败'); }
    } catch (e) { alert('删除出错: ' + e.message); }
}

function showKbAddForm() {
    document.getElementById('kbAddForm').style.display = 'block';
    document.getElementById('kbAddFormTitle').textContent = '➕ 新增条目';
    delete document.getElementById('kbAddForm').dataset.editCategory;
    delete document.getElementById('kbAddForm').dataset.editId;
    document.getElementById('kbAddQuestion').value = '';
    document.getElementById('kbAddKeywords').value = '';
    document.getElementById('kbAddAnswer').value = '';
    document.getElementById('kbAddFollowup').value = '';
}

async function doKbAdd() {
    const form = document.getElementById('kbAddForm');
    const editCategory = form.dataset.editCategory;
    const editId = form.dataset.editId;
    const category = document.getElementById('kbAddCategory').value;
    const question = document.getElementById('kbAddQuestion').value.trim();
    const keywordsStr = document.getElementById('kbAddKeywords').value.trim();
    const answer = document.getElementById('kbAddAnswer').value.trim();
    const followUp = document.getElementById('kbAddFollowup').value.trim();
    if (!question || !keywordsStr || !answer) { alert('问题、关键词和回答不能为空'); return; }
    const keywords = keywordsStr.split(/[,，]/).map(k=>k.trim()).filter(Boolean);
    const item = {question, keywords, answer};
    if (followUp) item.follow_up = followUp;
    try {
        if (editCategory && editId) {
            // 编辑模式：删旧+加新（JSON顺序保持）
            const delResp = await api('/api/knowledge_base/items?category='+editCategory+'&index='+editId, {method:'DELETE'});
            const delData = await delResp.json();
            if (!delData.success) { alert('编辑失败: 旧条目更新出错'); return; }
        }
        const resp = await api('/api/knowledge_base/items', {
            method: 'POST',
            body: JSON.stringify({category, item})
        });
        const data = await resp.json();
        if (data.success) {
            form.style.display = 'none';
            loadKnowledge();
        } else { alert('保存失败: ' + (data.error||'')); }
    } catch (e) { alert('保存出错: ' + e.message); }
}

async function doKbDelete(category, index) {
    if (!confirm('确定删除这条知识库条目吗？')) return;
    try {
        const resp = await api('/api/knowledge_base/items?category='+category+'&index='+index, {method:'DELETE'});
        const data = await resp.json();
        if (data.success) { loadKnowledge(); } else { alert('删除失败'); }
    } catch (e) { alert('删除出错: ' + e.message); }
}
(async function init() {
    // 设置默认日期筛选
    const today = new Date().toISOString().slice(0, 10);
    const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);
    document.getElementById('svcStartDate').value = weekAgo;
    document.getElementById('svcEndDate').value = today;
    document.getElementById('saleStartDate').value = weekAgo;
    document.getElementById('saleEndDate').value = today;

    // 检查 token 有效性
    if (token) {
        try {
            const resp = await api('/api/dashboard');
            if (resp.ok) { showApp(); return; }
        } catch (e) {}
    }
    showLogin();
})();
</script>
</body>
</html>"""
