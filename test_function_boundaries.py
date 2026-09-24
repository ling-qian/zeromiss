"""函数层全边界测试：覆盖所有业务函数的校验链与状态分支

与 test_reply_guard.py / test_dialog_regression.py 共同构成三层测试：
函数层（本文件，无LLM、秒级）、审查器层（无LLM）、对话层（真实LLM）。
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mktemp(suffix=".db")

from database.manager import DatabaseManager
from config import business_functions as bf

db = DatabaseManager(f"sqlite:///{_tmp}")
db.create_tables()
def setup_module(module):
    """执行阶段重设全局 db：pytest 收集期会 import 全部测试文件，
    后 import 文件的模块级 set_db 会覆盖本文件的库，导致会员卡等函数读写错库。"""
    bf.set_db(db)



def u(c):
    """取会员卡当前状态"""
    from database.models import Membership
    with db.get_session() as s:
        m = s.query(Membership).filter(Membership.id == c).first()
        if not m:
            return None
        s.refresh(m)
        return {"is_active": m.is_active, "balance": float(m.balance),
                "remaining": m.remaining_sessions,
                "extra": m.extra_data or {}}


class TestOpenMembership(unittest.TestCase):
    def test_empty_name_rejected(self):
        for name in ("", "   ", None):
            r = bf.open_membership(name, "储值卡", 500)
            self.assertFalse(r["success"], f"空名 {name!r} 应拒绝")

    def test_unknown_card_rejected(self):
        r = bf.open_membership("测试甲", "钻石卡", 500)
        self.assertFalse(r["success"])
        self.assertIn("支持的卡型", r["error"])

    def test_zero_negative_amount_rejected(self):
        for amt in (0, -100):
            r = bf.open_membership("测试乙", "储值卡", amt)
            self.assertFalse(r["success"])

    def test_session_card_requires_sessions(self):
        r = bf.open_membership("测试丙", "次卡", 500)
        self.assertFalse(r["success"])
        self.assertIn("sessions", r["error"])

    def test_session_card_bad_sessions(self):
        for s in (0, -3):
            r = bf.open_membership("测试丙", "次卡", 500, sessions=s)
            self.assertFalse(r["success"])

    def test_session_card_state(self):
        r = bf.open_membership("次卡甲", "次卡", 500, sessions=10)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["total_sessions"], 10)
        st = u(r["membership_id"])
        self.assertEqual(st["balance"], 0)          # 次卡不存钱
        self.assertEqual(st["remaining"], 10)
        self.assertEqual(st["extra"]["total_sessions"], 10)  # 退款折算依据

    def test_recharge_bonus_tiers(self):
        cases = {500: 550, 1000: 1150, 2000: 2400}
        for i, (amt, expect) in enumerate(cases.items()):
            r = bf.open_membership(f"储值{i}", "储值卡", amt)
            self.assertTrue(r["success"], r)
            self.assertEqual(u(r["membership_id"])["balance"], expect,
                             f"充{amt}应到账{expect}")

    def test_below_tier_no_bonus(self):
        r = bf.open_membership("储值小", "储值卡", 499)
        self.assertEqual(u(r["membership_id"])["balance"], 499)

    def test_month_card_points(self):
        r = bf.open_membership("积分甲", "月卡", 500)
        self.assertEqual(r["points"], 60)  # 月卡 0.12

    def test_invalid_date_rejected(self):
        for d in ("明天", "2026/13/45", "abc"):
            r = bf.open_membership("日期甲", "储值卡", 100, date_str=d)
            self.assertFalse(r["success"], f"垃圾日期 {d!r} 应拒绝")
            self.assertIn("日期", r["error"])

    def test_backdate_ok(self):
        old = (date.today() - timedelta(days=10)).isoformat()
        r = bf.open_membership("回溯甲", "次卡", 300, sessions=5, date_str=old)
        self.assertTrue(r["success"], r)

    def test_treatment_card_allowed(self):
        r = bf.open_membership("疗程甲", "疗程卡", 2000, sessions=20)
        self.assertTrue(r["success"], r)


class TestRedeemSession(unittest.TestCase):
    def setUp(self):
        r = bf.open_membership("核销甲", "次卡", 500, sessions=3)
        self.mid = r["membership_id"]

    def test_normal_redeem(self):
        r = bf.redeem_session(self.mid, "洗剪")
        self.assertTrue(r["success"], r)
        self.assertEqual(u(self.mid)["remaining"], 2)

    def test_redeem_to_zero_ok(self):
        for _ in range(3):
            r = bf.redeem_session(self.mid)
        self.assertTrue(r["success"])
        self.assertEqual(u(self.mid)["remaining"], 0)
        r = bf.redeem_session(self.mid)
        self.assertFalse(r["success"])  # 第4次拒绝

    def test_inactive_rejected(self):
        bf.redeem_session(self.mid)
        mid = self.mid
        from database.models import Membership
        with db.get_session() as s:
            m = s.query(Membership).filter(Membership.id == mid).first()
            m.is_active = False
            s.commit()
        r = bf.redeem_session(mid)
        self.assertFalse(r["success"])

    def test_expired_rejected(self):
        mid = self.mid
        from database.models import Membership
        with db.get_session() as s:
            m = s.query(Membership).filter(Membership.id == mid).first()
            m.expires_at = date.today() - timedelta(days=1)
            s.commit()
        r = bf.redeem_session(mid)
        self.assertFalse(r["success"])
        self.assertIn("到期", r.get("error", ""))

    def test_non_session_card_rejected(self):
        r = bf.open_membership("储值核销", "储值卡", 500)
        r = bf.redeem_session(r["membership_id"])
        self.assertFalse(r["success"])  # 储值卡应提示走扣余额

    def test_not_found(self):
        r = bf.redeem_session(999999)
        self.assertFalse(r["success"])


class TestRefundMembership(unittest.TestCase):
    def test_cooldown_full_refund(self):
        r = bf.open_membership("退款甲", "储值卡", 500)
        r = bf.refund_membership(r["membership_id"], "不想要了")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["refund_amount"], 500)
        st = u(r.get("membership_id") or 0) if r.get("membership_id") else None

    def test_over_cooldown_refund_balance(self):
        old = (date.today() - timedelta(days=10)).isoformat()
        r = bf.open_membership("退款乙", "储值卡", 1000, date_str=old)
        r = bf.refund_membership(r["membership_id"])
        self.assertTrue(r["success"], r)
        self.assertEqual(float(r["refund_amount"]), 1150)  # 未消费退余额(含赠送150)

    def test_session_card_full_refund_in_cooldown(self):
        r = bf.open_membership("退款丙", "次卡", 500, sessions=10)
        r = bf.refund_membership(r["membership_id"])
        self.assertTrue(r["success"], r)
        self.assertEqual(r["refund_amount"], 500)  # 未核销全额

    def test_session_card_ratio_refund(self):
        r = bf.open_membership("退款丁", "次卡", 500, sessions=10)
        mid = r["membership_id"]
        bf.redeem_session(mid)  # 用1次
        bf.redeem_session(mid)  # 用2次
        bf.redeem_session(mid)  # 用3次
        bf.redeem_session(mid)  # 用4次
        r = bf.refund_membership(mid)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["refund_amount"], 300)  # 500*6/10
        st = u(mid)
        self.assertEqual(st["remaining"], 0)
        self.assertFalse(st["is_active"])

    def test_session_card_used_up(self):
        r = bf.open_membership("退款戊", "次卡", 500, sessions=2)
        mid = r["membership_id"]
        bf.redeem_session(mid); bf.redeem_session(mid)
        r = bf.refund_membership(mid)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["refund_amount"], 0)

    def test_legacy_session_card_manual(self):
        """老数据缺 total_sessions：停卡防误核销 + 转人工"""
        r = bf.open_membership("退款己", "次卡", 500, sessions=5)
        mid = r["membership_id"]
        from database.models import Membership
        with db.get_session() as s:
            m = s.query(Membership).filter(Membership.id == mid).first()
            m.extra_data = {}
            s.commit()
        r = bf.refund_membership(mid)
        self.assertFalse(r["success"])  # 转人工（不自动折算）
        self.assertFalse(u(mid)["is_active"])  # 但已停卡防继续核销

    def test_double_refund_rejected(self):
        r = bf.open_membership("退款庚", "储值卡", 300)
        mid = r["membership_id"]
        self.assertTrue(bf.refund_membership(mid)["success"])
        r = bf.refund_membership(mid)
        self.assertFalse(r["success"])
        self.assertIn("失效", r["error"])

    def test_not_found(self):
        r = bf.refund_membership(999999)
        self.assertFalse(r["success"])


class TestDeductAndPoints(unittest.TestCase):
    def test_deduct_normal(self):
        r = bf.open_membership("扣款甲", "储值卡", 500)
        mid = r["membership_id"]
        r = bf.deduct_membership_balance(mid, 100)
        self.assertTrue(r["success"], r)
        self.assertEqual(float(u(mid)["balance"]), 450)  # 550(含赠送)-100

    def test_deduct_insufficient(self):
        r = bf.open_membership("扣款乙", "储值卡", 100)
        r = bf.deduct_membership_balance(r["membership_id"], 999)
        self.assertFalse(r["success"])

    def test_redeem_points_ok(self):
        r = bf.open_membership("积分乙", "储值卡", 1000)  # 100分
        r = bf.redeem_points("积分乙", 100)
        self.assertTrue(r["success"], r)

    def test_redeem_points_not_multiple(self):
        bf.open_membership("积分丙", "储值卡", 1000)
        for p in (50, 33, -100, 0):
            r = bf.redeem_points("积分丙", p)
            self.assertFalse(r["success"], f"{p} 应拒绝")

    def test_redeem_points_over_hold(self):
        bf.open_membership("积分丁", "储值卡", 500)  # 50分
        r = bf.redeem_points("积分丁", 100)
        self.assertFalse(r["success"])


class TestServiceAndProduct(unittest.TestCase):
    def test_service_normal(self):
        r = bf.record_service("服务甲", "染发", 168, employee_name="阿杰")
        self.assertTrue(r["success"], r)

    def test_service_zero_amount(self):
        r = bf.record_service("服务甲", "染发", 0)
        self.assertFalse(r["success"])

    def test_service_empty_name(self):
        r = bf.record_service("", "染发", 100)
        self.assertFalse(r["success"])

    def test_product_sale_and_stock(self):
        bf.add_product("测试发膜", unit_price=88, stock_quantity=5)
        r = bf.record_product_sale("测试发膜", 176, quantity=2)
        self.assertTrue(r["success"], r)
        r = bf.list_products()
        item = next((p for p in r.get("products", []) if "测试发膜" in str(p.get("name", ""))), None)
        if item:
            self.assertEqual(item["stock_quantity"], 3)

    def test_product_insufficient_stock(self):
        r = bf.record_product_sale("测试发膜", 99900, quantity=999)
        self.assertFalse(r["success"])

    def test_product_not_found(self):
        r = bf.record_product_sale("不存在的商品XYZ", 1)
        self.assertFalse(r["success"])


class TestQueriesAndDates(unittest.TestCase):
    def test_parse_date_garbage_raises(self):
        for d in ("明天", "2026/13/45", "abc", "2026-02-30"):
            with self.assertRaises(ValueError):
                bf._parse_date(d)

    def test_parse_date_none_is_today(self):
        self.assertEqual(bf._parse_date(None), date.today())

    def test_daily_summary(self):
        r = bf.query_daily_summary()
        self.assertTrue(r.get("success", True), r)

    def test_expiring_members(self):
        r = bf.open_membership("到期甲", "月卡", 100)  # 30天到期
        r = bf.query_expiring_members(days=31)
        self.assertTrue(r.get("success", True), r)

    def test_low_stock(self):
        r = bf.query_low_stock_products()
        self.assertTrue(r.get("success", True), r)

    def test_search_customers(self):
        r = bf.search_customers("服务甲")
        self.assertTrue(r.get("success", True), r)

    def test_employee_lifecycle(self):
        r = bf.add_employee("测试员工TT", role="技师", commission_rate=20)
        self.assertTrue(r.get("success", True), r)


if __name__ == "__main__":
    unittest.main(verbosity=1)
