"""增值项功能测试：预约落库 / 会话消息存档 / 转人工检测 / Web API。

覆盖：
- create_appointment / list_appointments / update_appointment_status 边界
- ChatMessageRepository 存取与会话聚合
- detect_needs_human 关键词检测
- Web API：/api/appointments、/api/chat_sessions、/api/chat_messages（含鉴权）

运行：python3 test_value_added.py
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

DB_URL = f'sqlite:///{tempfile.mktemp(suffix=".db")}'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger

logger.remove()

from database.manager import DatabaseManager
from config import business_functions as bf
from agent.agent import detect_needs_human
from interface.web.channel import WebChannel

db = DatabaseManager(database_url=DB_URL)
db.create_tables()
def setup_module(module):
    """执行阶段重设全局 db：pytest 收集期会 import 全部测试文件，
    后 import 文件的模块级 set_db 会覆盖本文件的库，导致会员卡等函数读写错库。"""
    bf.set_db(db)



class TestAppointmentFunctions(unittest.TestCase):
    """预约函数边界。"""

    def test_01_create_basic(self):
        r = bf.create_appointment(
            customer_name="张姐", service_name="剪发",
            appointment_date="2026-09-20", appointment_time="14:30",
        )
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], "pending")
        self.assertIn("张姐", r["message"])

    def test_02_create_default_today(self):
        r = bf.create_appointment(customer_name="李姐", service_name="染发")
        self.assertTrue(r["success"])
        self.assertEqual(r["appointment_date"], date.today().isoformat())

    def test_03_create_empty_name(self):
        r = bf.create_appointment(customer_name="   ", service_name="剪发")
        self.assertFalse(r["success"])
        self.assertIn("姓名", r["error"])

    def test_04_create_empty_service(self):
        r = bf.create_appointment(customer_name="王姐", service_name="")
        self.assertFalse(r["success"])
        self.assertIn("服务项目", r["error"])

    def test_05_create_bad_date(self):
        r = bf.create_appointment(
            customer_name="王姐", service_name="剪发", appointment_date="明天"
        )
        self.assertFalse(r["success"])
        self.assertIn("YYYY-MM-DD", r["error"])

    def test_06_create_bad_time_is_kept_as_note(self):
        """时间格式不强制校验（顾客说'下午'也能存），宽松处理。"""
        r = bf.create_appointment(
            customer_name="赵姐", service_name="烫发", appointment_time="下午"
        )
        self.assertTrue(r["success"])
        self.assertEqual(r["appointment_time"], "下午")

    def test_07_link_existing_customer(self):
        """同名已建档顾客自动关联 customer_id 和电话。"""
        db.customers.get_or_create(name="关联姐")
        with db.get_session() as sess:
            from database.models import Customer
            cust = sess.query(Customer).filter(Customer.name == "关联姐").first()
            cust.phone = "13800001111"
            sess.commit()
        r = bf.create_appointment(
            customer_name="关联姐", service_name="护理", appointment_date="2026-09-21"
        )
        self.assertTrue(r["success"])
        appt = db.appointments.list_appointments(target_date=date(2026, 9, 21))[0]
        self.assertIsNotNone(appt.customer_id)
        self.assertEqual(appt.phone, "13800001111")

    def test_08_list_by_date(self):
        r = bf.list_appointments(date_str="2026-09-20")
        self.assertTrue(r["success"])
        # 日期过滤必须精确：所有返回条目都在该日，且包含最早创建的张姐
        self.assertTrue(all(a["date"] == "2026-09-20" for a in r["appointments"]))
        self.assertIn("张姐", [a["customer_name"] for a in r["appointments"]])

    def test_09_list_upcoming_range(self):
        r = bf.list_appointments()
        self.assertTrue(r["success"])
        self.assertGreaterEqual(r["count"], 2)

    def test_10_list_bad_date(self):
        r = bf.list_appointments(date_str="09/20")
        self.assertFalse(r["success"])

    def test_11_update_status_valid(self):
        r = bf.create_appointment(customer_name="钱姐", service_name="接发")
        r2 = bf.update_appointment_status(r["appointment_id"], "confirmed")
        self.assertTrue(r2["success"])
        self.assertEqual(r2["status"], "confirmed")
        r3 = bf.update_appointment_status(r["appointment_id"], "completed")
        self.assertEqual(r3["status"], "completed")

    def test_12_update_status_invalid(self):
        r = bf.create_appointment(customer_name="孙姐", service_name="美甲")
        r2 = bf.update_appointment_status(r["appointment_id"], "随便")
        self.assertFalse(r2["success"])

    def test_13_update_not_found(self):
        r = bf.update_appointment_status(99999, "cancelled")
        self.assertFalse(r["success"])
        self.assertIn("未找到", r["error"])

    def test_14_cancel(self):
        r = bf.create_appointment(customer_name="周姐", service_name="染发")
        r2 = bf.update_appointment_status(r["appointment_id"], "cancelled")
        self.assertEqual(r2["status"], "cancelled")
        # cancelled 不影响再次查询
        r3 = bf.list_appointments(status="cancelled")
        self.assertGreaterEqual(r3["count"], 1)


class TestAppointmentGuards(unittest.TestCase):
    """预约三漏洞回归：过去日期/状态机/时段冲突。"""

    def setUp(self):
        from datetime import timedelta
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()

    def test_50_past_date_rejected(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        r = bf.create_appointment(customer_name="过去姐", service_name="剪发",
                                  appointment_date=yesterday)
        self.assertFalse(r["success"])
        self.assertIn("过去", r["error"])

    def test_51_today_allowed(self):
        r = bf.create_appointment(customer_name="当日姐", service_name="剪发",
                                  appointment_date=date.today().isoformat(),
                                  appointment_time="20:00")
        self.assertTrue(r["success"])

    def test_52_slot_conflict_rejected(self):
        bf.create_appointment(customer_name="占用客", service_name="剪发",
                              appointment_date=self.tomorrow, appointment_time="10:00")
        r = bf.create_appointment(customer_name="撞单客", service_name="染发",
                                  appointment_date=self.tomorrow, appointment_time="10:00")
        self.assertFalse(r["success"])
        self.assertIn("已有预约", r["error"])
        self.assertIn("占用客", r["error"])  # 冲突详情含已约顾客名

    def test_53_different_slot_ok(self):
        r = bf.create_appointment(customer_name="错峰客", service_name="剪发",
                                  appointment_date=self.tomorrow, appointment_time="11:30")
        self.assertTrue(r["success"])

    def test_54_cancelled_slot_released(self):
        r = bf.create_appointment(customer_name="释放客", service_name="剪发",
                                  appointment_date=self.tomorrow, appointment_time="12:00")
        bf.update_appointment_status(r["appointment_id"], "cancelled")
        r2 = bf.create_appointment(customer_name="补位客", service_name="剪发",
                                   appointment_date=self.tomorrow, appointment_time="12:00")
        self.assertTrue(r2["success"])

    def test_55_state_machine_happy_path(self):
        r = bf.create_appointment(customer_name="流转客", service_name="护理",
                                  appointment_date=self.tomorrow)
        aid = r["appointment_id"]
        self.assertTrue(bf.update_appointment_status(aid, "confirmed")["success"])
        self.assertTrue(bf.update_appointment_status(aid, "completed")["success"])

    def test_56_state_machine_no_rollback(self):
        r = bf.create_appointment(customer_name="回退客", service_name="烫发",
                                  appointment_date=self.tomorrow)
        aid = r["appointment_id"]
        bf.update_appointment_status(aid, "completed")
        for illegal in ("pending", "confirmed", "cancelled", "no_show"):
            resp = bf.update_appointment_status(aid, illegal)
            self.assertFalse(resp["success"], f"completed 不应能改为 {illegal}")

    def test_57_no_show_from_pending(self):
        r = bf.create_appointment(customer_name="爽约客", service_name="美甲",
                                  appointment_date=self.tomorrow)
        resp = bf.update_appointment_status(r["appointment_id"], "no_show")
        self.assertTrue(resp["success"])
        # no_show 是终态
        resp2 = bf.update_appointment_status(r["appointment_id"], "pending")
        self.assertFalse(resp2["success"])

    def test_58_repo_level_validation(self):
        """repo 层直接调用也有状态机校验（权威层）。"""
        r = bf.create_appointment(customer_name="直连客", service_name="护理",
                                  appointment_date=self.tomorrow)
        with self.assertRaises(ValueError):
            db.appointments.update_status(r["appointment_id"], "confirmed" if False else "completed")
            db.appointments.update_status(r["appointment_id"], "pending")


class TestChatMessageRepo(unittest.TestCase):
    """会话消息存档。"""

    def test_20_save_and_query(self):
        db.save_chat_message(session_id="wx_a", direction="in", content="你好",
                             sender_name="小明", channel="wechat-webhook")
        db.save_chat_message(session_id="wx_a", direction="out", content="欢迎！",
                             sender_name="AI助手", channel="wechat-webhook")
        msgs = db.get_chat_messages(session_id="wx_a")
        self.assertEqual(len(msgs), 2)
        self.assertEqual([m.direction for m in msgs], ["in", "out"])

    def test_21_invalid_direction(self):
        with self.assertRaises(ValueError):
            db.save_chat_message(session_id="wx_a", direction="x", content="?")

    def test_22_empty_content(self):
        with self.assertRaises(ValueError):
            db.save_chat_message(session_id="wx_a", direction="in", content="")

    def test_23_sessions_aggregate(self):
        db.save_chat_message(session_id="wx_b", direction="in", content="帮我约明天",
                             sender_name="小红", needs_human=False)
        db.save_chat_message(session_id="wx_b", direction="out",
                             content="得店主说了算，我帮您问问",
                             sender_name="AI助手", needs_human=True)
        sessions = db.get_chat_sessions()
        target = [s for s in sessions if s["session_id"] == "wx_b"][0]
        self.assertEqual(target["message_count"], 2)
        self.assertEqual(target["needs_human_count"], 1)
        self.assertIn("店主", target["last_content"])

    def test_24_filter_needs_human(self):
        rows = db.get_chat_messages(needs_human=True)
        self.assertTrue(all(m.needs_human for m in rows))
        self.assertGreaterEqual(len(rows), 1)

    def test_25_limit(self):
        for i in range(12):
            db.save_chat_message(session_id="wx_c", direction="in", content=f"m{i}")
        rows = db.get_chat_messages(session_id="wx_c", limit=5)
        self.assertEqual(len(rows), 5)
        # 时间正序：最后一条应是 m11
        self.assertEqual(rows[-1].content, "m11")


class TestNeedsHumanDetection(unittest.TestCase):
    """转人工关键词检测。"""

    def test_30_positive(self):
        cases = [
            "这个需要联系店主处理哦",
            "具体折扣得店主说了算",       # 审查器安全话术 → 应标红
            "我帮您问问店长",
            "要不您到店详谈？",
            "您加店主微信细聊",
        ]
        for c in cases:
            self.assertTrue(detect_needs_human(c), f"应命中: {c}")

    def test_31_negative(self):
        cases = [
            "欢迎光临！男士剪发38元",
            "您的会员卡余额是500元",
            "明天下午2点可以吗",
            "",
        ]
        for c in cases:
            self.assertFalse(detect_needs_human(c), f"不应命中: {c}")

    def test_32_normal_reply_not_flagged(self):
        """正常业务回复即使包含'店'字也不误伤。"""
        self.assertFalse(detect_needs_human("本店共有3位技师"))


class TestWebAPIs(unittest.TestCase):
    """Web API（ASGI 异步内存测试）。"""

    @classmethod
    def setUpClass(cls):
        cls.web = WebChannel(
            secret_key="sk", username="boss", password="pw123", db_manager=db
        )
        cls.app = cls.web._create_app()
        # 预置数据
        db.save_chat_message(session_id="wx_web", direction="in", content="在吗",
                             sender_name="测试顾客", channel="wechat")
        db.save_chat_message(session_id="wx_web", direction="out", content="在的姐",
                             sender_name="AI助手", needs_human=False)
        db.save_chat_message(session_id="wx_web2", direction="in", content="能便宜点吗",
                             sender_name="砍价顾客", needs_human=True)

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    async def _api(self, path, method="GET", json_body=None, auth=True):
        import httpx
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/login", json={"username": "boss", "password": "pw123"}
            )
            tok = resp.json()["token"]
            headers = {"Authorization": f"Bearer {tok}"} if auth else {}
            if method == "GET":
                return await client.get(path, headers=headers)
            return await client.put(path, json=json_body, headers=headers)

    def test_40_appointments_requires_auth(self):
        resp = self._run(self._api("/api/appointments", auth=False))
        self.assertEqual(resp.status_code, 401)

    def test_41_appointments_list(self):
        resp = self._run(self._api("/api/appointments"))
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json()["data"], list)

    def test_42_appointments_date_filter(self):
        resp = self._run(self._api("/api/appointments?date=2026-09-20"))
        data = resp.json()["data"]
        self.assertTrue(all(d["date"] == "2026-09-20" for d in data))
        self.assertIn("张姐", [d["customer_name"] for d in data])

    def test_43_appointment_status_update(self):
        r = bf.create_appointment(customer_name="API姐", service_name="剪发")
        resp = self._run(self._api(
            f"/api/appointments/{r['appointment_id']}/status", method="PUT",
            json_body={"status": "confirmed"},
        ))
        self.assertTrue(resp.json()["success"])

    def test_44_appointment_status_bad(self):
        resp = self._run(self._api(
            "/api/appointments/1/status", method="PUT", json_body={"status": "乱写"},
        ))
        self.assertFalse(resp.json()["success"])

    def test_45_chat_sessions_requires_auth(self):
        resp = self._run(self._api("/api/chat_sessions", auth=False))
        self.assertEqual(resp.status_code, 401)

    def test_46_chat_sessions(self):
        resp = self._run(self._api("/api/chat_sessions"))
        sessions = resp.json()["data"]
        self.assertGreaterEqual(len(sessions), 2)
        ids = [s["session_id"] for s in sessions]
        self.assertIn("wx_web", ids)
        self.assertIn("wx_web2", ids)

    def test_47_chat_messages_requires_auth(self):
        resp = self._run(self._api("/api/chat_messages?session_id=wx_web", auth=False))
        self.assertEqual(resp.status_code, 401)

    def test_48_chat_messages(self):
        resp = self._run(self._api("/api/chat_messages?session_id=wx_web"))
        msgs = resp.json()["data"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["direction"], "in")
        self.assertEqual(msgs[0]["sender_name"], "测试顾客")

    def test_49_messages_no_session(self):
        resp = self._run(self._api("/api/chat_messages?limit=10"))
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=1)
