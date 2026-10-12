import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_commerce as base
from app.commerce.rules import RULES, evaluate, load_rules
from app.commerce.knowledge import PRODUCTS
from app.commerce.store import Order


class BusinessRulesTests(unittest.TestCase):
    setUp = base.CommerceTests.setUp
    tearDown = base.CommerceTests.tearDown
    account = base.CommerceTests.account
    chat = base.CommerceTests.chat
    mutate = base.CommerceTests.mutate
    prepare = base.CommerceTests.prepare
    confirm = base.CommerceTests.confirm

    def details(self, identifier, **changes):
        with self.store.session.begin() as db:
            order = db.get(Order, identifier)
            order.details = {**order.details, **changes}

    def test_all_nine_rule_topics_are_queryable_and_sourced(self):
        queries = {"库存限购规则": "purchase", "优惠券怎么用": "coupon", "包邮规则": "shipping_fee",
            "多久发货": "dispatch", "修改地址规则": "address_change", "取消规则": "cancellation",
            "发票规则": "invoice", "保价规则": "price_protection", "物流不更新怎么办": "logistics_delay"}
        for i, (query, topic) in enumerate(queries.items()):
            with self.subTest(topic=topic):
                result = self.chat(query, conversation=f"topic{i}")
                self.assertEqual(result["intent"], "rule_query")
                self.assertEqual(result["status"], "policy_info", result)
                self.assertEqual(result["data"]["topic"], topic)
                self.assertTrue(result["sources"])
                self.assertEqual(result["tool_trace"][0]["tool"], "check_business_rule")

    def test_stock_and_limit_boundaries(self):
        for product, quantity, status in [("P100",5,"eligible"),("P100",6,"not_eligible"),("P300",4,"not_eligible")]:
            result = self.chat(f"{product} 买 {quantity} 件有货吗", conversation=f"qty{quantity}{product}")
            self.assertEqual(result["status"], status, result)

    def test_missing_quantity_followup_keeps_product(self):
        self.assertEqual(self.chat("P100 有货吗")["status"], "clarification_required")
        result = self.chat("买2件")
        self.assertEqual(result["status"], "eligible", result)
        self.assertEqual(result["data"]["facts"]["quantity"], 2)

    def test_coupon_threshold_and_exclusions(self):
        for product, quantity, status, discount in [("P100",1,"not_eligible",0),("P100",2,"eligible",2000),("P300",3,"not_eligible",None)]:
            result = self.chat(f"{product} 买{quantity}件能用优惠券吗", conversation=f"coupon{product}{quantity}")
            self.assertEqual(result["status"], status)
            if discount is not None:
                self.assertEqual(result["data"]["facts"]["discount_cents"], discount)
        product = {**PRODUCTS["P100"], "price_cents": 20000}
        self.assertEqual(evaluate("coupon", product, quantity=1)["facts"]["discount_cents"], 2000)

    def test_order_discount_cannot_stack(self):
        identifier = self.by_status["paid"]
        self.details(identifier, merchandise_cents=39800, discount_cents=1000)
        result = self.chat(f"{identifier} 能叠加优惠券吗")
        self.assertEqual(result["status"], "not_eligible")

    def test_freight_region_amount_and_followup(self):
        self.assertEqual(self.chat("P200 买1件运费多少")["status"], "clarification_required")
        result = self.chat("普通区域")
        self.assertEqual(result["data"]["facts"]["shipping_fee_cents"], 800)
        result = self.chat("那新疆呢")
        self.assertEqual(result["data"]["facts"]["shipping_fee_cents"], 2000)
        result = self.chat("P100 买1件普通区域包邮吗", conversation="free")
        self.assertEqual(result["data"]["facts"]["shipping_fee_cents"], 0)
        result = self.chat("P100 买1件海外包邮吗", conversation="overseas")
        self.assertEqual(result["status"], "not_eligible")

    def test_shipping_region_cannot_override_order(self):
        result = self.chat(f"订单 {self.by_status['paid']} 新疆包邮吗")
        self.assertEqual(result["status"], "clarification_required")

    def test_dispatch_deadlines_and_missing_timestamp(self):
        identifier = self.by_status["paid"]
        self.assertEqual(self.chat(f"{identifier} 什么时候发货")["status"], "ok")
        self.details(identifier, paid_at=time.time()-49*3600)
        self.assertEqual(self.chat(f"{identifier} 什么时候发货")["status"], "handoff_required")
        self.mutate(identifier, product_id="P300")
        self.assertEqual(self.chat(f"{identifier} 什么时候发货")["status"], "ok")
        self.details(identifier, paid_at=None)
        self.assertEqual(self.chat(f"{identifier} 什么时候发货")["status"], "clarification_required")

    def test_address_rule_is_read_only(self):
        result = self.chat(f"订单 {self.by_status['paid']} 可以改地址吗")
        self.assertEqual(result["status"], "eligible")
        self.assertIn("不执行", result["answer"])
        self.assertNotIn("confirmation_id", result["data"])
        self.assertEqual(self.chat(f"订单 {self.by_status['shipped']} 可以改地址吗")["status"], "not_eligible")

    def test_cancellation_query_does_not_prepare_operation(self):
        result = self.chat(f"订单 {self.by_status['paid']} 能取消吗")
        self.assertEqual(result["status"], "eligible")
        self.assertNotIn("confirmation_id", result["data"])

    def test_custom_production_cancellation_blocked_at_commit(self):
        identifier = self.by_status["paid"]
        self.mutate(identifier, product_id="P300")
        confirmation = self.prepare()
        self.details(identifier, production_started=True)
        self.assertEqual(self.confirm(confirmation).json()["code"], "invalid_state")
        self.assertEqual(self.chat(f"取消订单 {identifier}")["status"], "invalid_state")

    def test_invoice_amount_is_historical_paid_amount(self):
        identifier = self.by_status["delivered"]
        self.details(identifier, paid_merchandise_cents=5900)
        result = self.chat(f"{identifier} 能开发票吗")
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["data"]["facts"]["invoice_amount_cents"], 5900)
        self.details(identifier, invoice_status="issued")
        self.assertEqual(self.chat(f"{identifier} 能开发票吗")["status"], "not_eligible")

    def test_price_protection_diff_and_expiry(self):
        identifier = self.by_status["delivered"]
        self.details(identifier, quantity=2, paid_merchandise_cents=19800, paid_at=time.time()-3600)
        result = self.chat(f"{identifier} 能保价吗")
        self.assertEqual(result["data"]["facts"]["difference_cents"], 4000)
        self.assertIn("未提交", result["answer"])
        self.details(identifier, paid_at=time.time()-169*3600)
        self.assertEqual(self.chat(f"{identifier} 能保价吗")["status"], "not_eligible")

    def test_logistics_stagnation(self):
        identifier = self.by_status["shipped"]
        self.assertEqual(self.chat(f"{identifier} 物流不更新")["status"], "ok")
        self.details(identifier, last_logistics_at=time.time()-49*3600)
        self.assertEqual(self.chat(f"{identifier} 物流不更新")["status"], "handoff_required")
        self.assertEqual(self.chat(f"{self.by_status['delivered']} 显示签收但没收到")["status"], "handoff_required")

    def test_rule_queries_enforce_ownership(self):
        for query in ["发票", "保价", "改地址", "运费", "能取消吗"]:
            result = self.chat(f"{self.by_status['paid']} {query}", headers=self.other)
            self.assertEqual(result["status"], "order_access_denied", result)

    def test_multiple_rule_topics_clarify(self):
        result = self.chat("P100 买2件能用优惠券吗，运费多少")
        self.assertEqual(result["status"], "clarification_required")
        self.assertFalse(result["tool_trace"])

    def test_product_switch_does_not_inherit_order(self):
        self.chat(f"查询订单 {self.by_status['paid']}")
        result = self.chat("P200 买1件能用优惠吗")
        self.assertIsNone(result["data"]["order_id"])
        self.assertEqual(result["data"]["facts"]["merchandise_cents"], 7900)

    def test_missing_historical_fields_not_guessed(self):
        identifier = self.by_status["delivered"]
        self.details(identifier, paid_merchandise_cents=None)
        for query in ["发票", "保价", "包邮"]:
            self.assertEqual(self.chat(f"{identifier} {query}")["status"], "clarification_required")

    def test_invalid_rule_parameters_and_overlap_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"rules.json"
            rule = json.loads(json.dumps(RULES[0]))
            rule["parameters"]["max_quantity"] = -1
            path.write_text(json.dumps([rule]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_rules(path)
            first = RULES[0]
            second = {**first, "version": "PURCHASE-DEMO-v2"}
            path.write_text(json.dumps([first, second]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_rules(path)

    def test_unknown_pinned_version_does_not_fall_back(self):
        identifier = self.by_status["paid"]
        self.details(identifier, rule_versions={"invoice": "MISSING"})
        self.assertEqual(self.chat(f"{identifier} 发票")["status"], "handoff_required")
