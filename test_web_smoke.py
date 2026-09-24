"""Web 层冒烟测试：登录认证、核心API、静态页面加载"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"
os.environ["JWT_SECRET_KEY"] = "testkey"

import asyncio
import httpx

from database.manager import DatabaseManager
from config import business_functions as bf

db = DatabaseManager(f"sqlite:///{_tmp}")
db.create_tables()
bf.set_db(db)
bf.record_service("冒烟甲", "染发", 168, employee_name="阿杰")
bf.open_membership("冒烟乙", "储值卡", 1000)

from interface.web.channel import WebChannel


async def main():
    wc = WebChannel(secret_key="sk", username="boss", password="pw123", db_manager=db)
    app = wc._create_app()
    passed, failed = 0, 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"P  {name}")
        else:
            failed += 1
            print(f"F  {name} -> {detail}")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # 1. 主页面（SPA壳公开，认证由API层+前端token控制）
        r = await c.get("/")
        check("SPA页面壳加载", r.status_code == 200 and "html" in r.text.lower()[:200])
        # 2. 登录：错误密码被拒
        r = await c.post("/api/login", json={"username": "boss", "password": "wrong"})
        check("错误密码被拒", r.status_code in (400, 401, 403), f"{r.status_code} {r.text[:60]}")
        # 3. 登录：正确密码
        r = await c.post("/api/login", json={"username": "boss", "password": "pw123"})
        ok = r.status_code == 200
        token = ""
        if ok:
            data = r.json()
            token = data.get("token") or data.get("access_token") or ""
            ok = bool(token)
        check("正确密码登录返回token", ok, r.text[:80])
        headers = {"Authorization": f"Bearer {token}"}
        # 4. 主页面加载
        r = await c.get("/", headers=headers)
        check("主面板加载", r.status_code == 200, str(r.status_code))
        # 5. AI功劳簿API
        r = await c.get("/api/ai_contributions", headers=headers)
        ok = r.status_code == 200
        if ok:
            data = r.json()
            ok = any(k in data for k in ("month_records", "records", "minutes_saved"))
        check("AI功劳簿API", ok, r.text[:100])
        # 6. 看板API
        r = await c.get("/api/dashboard", headers=headers)
        check("看板API", r.status_code == 200, r.text[:80])
        # 7. 顾客API
        r = await c.get("/api/customers", headers=headers)
        check("顾客API", r.status_code == 200, r.text[:80])
        # 8. 会员API
        r = await c.get("/api/memberships", headers=headers)
        check("会员API", r.status_code == 200, r.text[:80])
        # 9. 知识库API
        r = await c.get("/api/knowledge_base", headers=headers)
        check("知识库API", r.status_code == 200, r.text[:80])
        # 10. 无token访问被拒
        r = await c.get("/api/dashboard")
        check("无token访问被拒", r.status_code in (401, 403), str(r.status_code))
        # 11. 健康检查
        r = await c.get("/health")
        check("健康检查", r.status_code == 200, str(r.status_code))
        # 12. 看板含备份状态字段（P0-2）
        r = await c.get("/api/dashboard", headers=headers)
        ok = r.status_code == 200
        if ok:
            data = r.json()
            ok = "backup" in data  # 可能为 None（未备份过），但字段必须存在
        check("看板返回备份状态字段", ok, r.text[:100])
        # 13. 登录防爆破：连续5次失败后锁定（P1-3）
        for _ in range(5):
            await c.post("/api/login", json={"username": "boss", "password": "nope"})
        r = await c.post("/api/login", json={"username": "boss", "password": "pw123"})
        check("连续失败后正确密码也被临时锁定", r.status_code == 429,
              f"{r.status_code} {r.text[:60]}")

    print(f"\n═══ Web冒烟: {passed} 通过 / {failed} 失败 ═══")


asyncio.run(main())
