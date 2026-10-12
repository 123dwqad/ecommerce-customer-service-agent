import unittest
from sqlalchemy import select, func
from app.commerce.agent import INTENTS, detect_intent
from app.commerce.store import AfterSale, Confirmation, Handoff
from app.commerce.db_models import AfterSaleDetail, AfterSaleEvent
import test_commerce as base


class IntentClassificationTests(unittest.TestCase):
    def test_all_ten_intents(self):
        cases = {"P100 耳机续航多久": "product_consult", "包邮规则": "rule_query",
            "我的订单": "order_query", "快递到哪了": "logistics_query", "这单还能退吗": "after_sales",
            "取消订单": "cancel_order", "我要退货": "create_after_sale", "我的售后进度": "after_sale_query",
            "转人工": "handoff", "你好": "other"}
        self.assertEqual(set(cases.values()), INTENTS)
        for query, intent in cases.items():
            with self.subTest(query=query):
                self.assertEqual(detect_intent(query), intent)

    def test_progress_policy_and_negation(self):
        cases = {"退款到账了吗": "after_sale_query", "我的退货申请审核通过了吗": "after_sale_query",
            "售后处理到哪了": "after_sale_query", "ASabcdef1234567890": "after_sale_query",
            "查询售后政策": "after_sales", "申请退货需要什么条件": "after_sales",
            "我不想取消订单，只想看看物流": "logistics_query",
            "不要帮我退货，我想问耳机续航": "product_consult", "不取消": "other",
            "先别帮我退货": "other"}
        for query, intent in cases.items():
            with self.subTest(query=query):
                self.assertEqual(detect_intent(query, "create_after_sale"), intent)
        self.assertEqual(detect_intent("审核通过了吗", "after_sale_query"), "after_sale_query")
        self.assertNotEqual(detect_intent("继续", "create_after_sale"), "create_after_sale")


class AfterSaleQueryTests(unittest.TestCase):
    setUp = base.CommerceTests.setUp
    tearDown = base.CommerceTests.tearDown
    account = base.CommerceTests.account
    chat = base.CommerceTests.chat
    prepare = base.CommerceTests.prepare
    confirm = base.CommerceTests.confirm

    def create_sale(self):
        prepared = self.prepare("我要退货", "delivered")
        return self.confirm(prepared).json()["after_sale_id"]

    def test_query_by_order_id_sale_id_and_followup_read_only(self):
        sid = self.create_sale()
        oid = self.by_status["delivered"]
        for query in [f"查询订单 {oid} 售后进度", f"售后单号 {sid} 处理到哪了", "审核通过了吗"]:
            result = self.chat(query)
            self.assertEqual(result["intent"], "after_sale_query")
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["data"]["after_sale"]["after_sale_id"], sid)
            self.assertIn("待审核", result["answer"])
            self.assertEqual([t["tool"] for t in result["tool_trace"]], ["query_after_sale"])
        with self.store.session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AfterSale)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Confirmation)), 1)
        for path in [f"/orders/{oid}/after-sales", f"/after-sales/{sid}"]:
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["after_sale"]["status"], "submitted")

    def test_no_id_clarifies_then_accepts_order_only_and_no_application(self):
        result = self.chat("查询售后进度")
        self.assertEqual(result["status"], "clarification_required")
        self.assertTrue(result["data"]["orders"])
        result = self.chat(self.by_status["paid"])
        self.assertEqual(result["intent"], "after_sale_query")
        self.assertEqual(result["status"], "no_after_sale")
        self.assertIn("暂无", result["answer"])

    def test_ownership_authentication_invalid_ids_and_mismatch(self):
        sid = self.create_sale()
        oid = self.by_status["delivered"]
        for path in [f"/orders/{oid}/after-sales", f"/after-sales/{sid}"]:
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.get(path, headers=self.other).status_code, 409)
        for query in [f"售后进度 {oid}", f"售后进度 {sid}"]:
            result = self.chat(query, headers=self.other)
            self.assertIn(result["status"], {"order_access_denied", "after_sale_access_denied"})
            self.assertNotIn("after_sale", result["data"])
        result = self.chat(f"售后进度 {self.by_status['paid']} {sid}")
        self.assertEqual(result["status"], "after_sale_order_mismatch")
        self.assertEqual(self.client.get("/after-sales/invalid", headers=self.headers).status_code, 422)
        self.assertEqual(self.chat("售后单号 XYZ") ["status"], "clarification_required")

    def test_switch_and_failed_lookup_do_not_reuse_old_application(self):
        sid = self.create_sale()
        self.chat(f"售后进度 {sid}")
        result = self.chat(f"售后进度 {self.by_status['paid']}")
        self.assertEqual(result["status"], "no_after_sale")
        self.chat("售后进度 ASNOTFOUND")
        self.assertEqual(self.chat("处理到哪了") ["status"], "clarification_required")
        self.chat(f"售后进度 {sid}")
        self.assertEqual(self.chat(f"售后进度 {sid} ASANOTHER") ["status"], "clarification_required")

    def test_all_statuses_and_legacy_missing_details(self):
        sid = self.create_sale()
        for status, label in [("submitted", "待审核"), ("approved", "审核通过"), ("returned", "退回商品"),
                              ("closed", "流程已关闭"), ("rejected", "未通过审核")]:
            with self.store.session.begin() as db:
                db.get(AfterSale, sid).status = status
            result = self.chat(f"查询售后进度 {sid}")
            self.assertIn(label, result["answer"])
            self.assertIn("不代表真实退款到账", result["answer"])
        with self.store.session.begin() as db:
            for event in db.scalars(select(AfterSaleEvent).where(AfterSaleEvent.after_sale_id == sid)):
                db.delete(event)
            db.delete(db.get(AfterSaleDetail, sid))
        result = self.chat(f"查询售后进度 {sid}")
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["data"]["after_sale"]["updated_at"])

    def test_negation_and_withdrawal_do_not_prepare_or_write(self):
        self.chat(f"查询订单 {self.by_status['paid']}")
        for query in ["我不想取消订单，只想看看物流", "先别帮我退货", "取消售后申请", "申请退货需要什么条件"]:
            result = self.chat(query)
            self.assertNotIn("prepare_operation", [t["tool"] for t in result["tool_trace"]])
        with self.store.session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Confirmation)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(AfterSale)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Handoff)), 0)
