"""Regression tests against real HTTP routes, transactions and graph execution (no paid APIs)."""
import asyncio
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.commerce.api import create_app
from app.commerce.config import Settings
from app.commerce.knowledge import POLICIES, load_policies
from app.commerce.service import BusinessError
from app.commerce.store import AfterSale, Confirmation, Order
from app.commerce.tools import sign_request, verify_request


class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(database_url="sqlite:///" + str(Path(self.temp.name) / "test.db"),
            redis_url="", tool_transport="local", llm_enabled=False, intent_enabled=False, demo_enabled=True)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = self.account("alice")
        self.other = self.account("bob")
        self.orders = self.client.get("/orders", headers=self.headers).json()["orders"]
        self.by_status = {o["status"]: o["order_id"] for o in self.orders}
        self.store = self.app.state.store
        self.actor = self.store.actor(self.headers["Authorization"][7:])

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def account(self, name):
        credentials = {"user_id": name, "password": "test-password-123"}
        self.assertEqual(self.client.post("/auth/register", json=credentials).status_code, 201)
        token = self.client.post("/auth/login", json=credentials).json()["access_token"]
        return {"Authorization": "Bearer " + token}

    def chat(self, text, headers=None, conversation="test"):
        response = self.client.post("/chat/", json={"query": text, "conversation_id": conversation}, headers=headers or self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prepare(self, action="取消订单", status="paid"):
        result = self.chat(f"{action} {self.by_status[status]}")
        self.assertEqual(result["status"], "confirmation_required", result)
        return result["data"]["confirmation_id"]

    def confirm(self, identifier, headers=None):
        return self.client.post("/operations/confirm", json={"confirmation_id": identifier}, headers=headers or self.headers)

    def mutate(self, order_id, **fields):
        with self.store.session.begin() as db:
            order = db.get(Order, order_id)
            for field, value in fields.items():
                setattr(order, field, value)

    def test_frontend_and_health_are_commerce(self):
        self.assertEqual(self.client.get("/health").json()["domain"], "commerce")
        self.assertIn("电商智能客服", self.client.get("/").text)

    def test_private_routes_require_authentication(self):
        for route in ["/orders", "/preferences", "/orders/UNKNOWN"]:
            self.assertEqual(self.client.get(route).status_code, 401)
        self.assertEqual(self.client.post("/chat/", json={"query": "订单"}).status_code, 401)

    def test_identity_cannot_be_overridden_by_body(self):
        result = self.client.post("/chat/", headers=self.headers, json={"query": "订单", "user_id": "bob"})
        self.assertEqual(result.status_code, 422)

    def test_ownership_enforced_in_api_and_agent(self):
        identifier = self.by_status["paid"]
        response = self.client.get(f"/orders/{identifier}", headers=self.other)
        self.assertEqual(response.status_code, 409)
        result = self.chat(f"取消订单 {identifier}", headers=self.other)
        self.assertEqual(result["status"], "order_access_denied")
        self.assertNotIn("confirmation_id", result["data"])

    def test_sensitive_fields_not_returned(self):
        result = json.dumps(self.chat(f"订单 {self.by_status['paid']}"), ensure_ascii=False)
        for sensitive in ["13800138000", "演示路", '"phone"', '"address"', '"recipient"']:
            self.assertNotIn(sensitive, result)

    def test_product_sources_and_no_results(self):
        result = self.chat("P100 耳机支持降噪吗")
        self.assertIn("不支持主动降噪", result["answer"])
        self.assertTrue(result["sources"])
        result = self.chat("火星飞船燃料", conversation="fresh")
        self.assertEqual(result["status"], "clarification_required")
        self.assertFalse(result["tool_trace"])

    def test_followup_resolves_current_order(self):
        identifier = self.by_status["delivered"]
        self.chat(f"查询订单 {identifier}")
        result = self.chat("这笔订单还能退吗")
        self.assertTrue(result["data"]["eligible"])
        self.assertEqual(result["data"]["order_id"], identifier)

    def test_switch_order_updates_context(self):
        self.chat(f"查询订单 {self.by_status['paid']}")
        self.chat(f"查询订单 {self.by_status['delivered']}")
        self.assertEqual(self.chat("这单物流")['data']['order_id'], self.by_status['delivered'])

    def test_multiple_orders_require_clarification(self):
        result = self.chat(f"取消订单 {self.by_status['paid']} 和 {self.by_status['shipped']}")
        self.assertEqual(result["status"], "clarification_required")
        self.assertFalse(result["tool_trace"])

    def test_invalid_order_does_not_reuse_previous(self):
        self.chat(f"查询订单 {self.by_status['paid']}")
        result = self.chat("取消订单 XXX")
        self.assertEqual(result["status"], "clarification_required")
        self.assertFalse(result["tool_trace"])

    def test_conversation_and_account_isolation(self):
        self.chat(f"查询订单 {self.by_status['delivered']}", conversation="first")
        result = self.chat("这单物流", conversation="second")
        self.assertEqual(result["status"], "clarification_required")
        result = self.chat("这单物流", headers=self.other, conversation="first")
        self.assertEqual(result["status"], "clarification_required")

    def test_cancel_requires_explicit_button_and_is_idempotent(self):
        identifier = self.prepare()
        self.chat("确认取消")
        with self.store.session() as db:
            self.assertEqual(db.get(Order, self.by_status["paid"]).status, "paid")
        first = self.confirm(identifier)
        second = self.confirm(identifier)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["status"], "completed")

    def test_confirmation_is_bound_to_login_session(self):
        identifier = self.prepare()
        self.assertEqual(self.confirm(identifier, self.other).status_code, 409)
        login = self.client.post("/auth/login", json={"user_id": "alice", "password": "test-password-123"}).json()
        self.assertEqual(self.confirm(identifier, {"Authorization": "Bearer " + login["access_token"]}).status_code, 409)

    def test_confirmation_expires(self):
        identifier = self.prepare()
        with self.store.session.begin() as db:
            db.get(Confirmation, identifier).expires = time.time() - 1
        self.assertEqual(self.confirm(identifier).json()["code"], "confirmation_expired")

    def test_confirmation_revalidates_order_version(self):
        identifier = self.prepare()
        self.mutate(self.by_status["paid"], version=2, status="shipped")
        self.assertEqual(self.confirm(identifier).json()["code"], "stale_confirmation")

    def test_shipped_order_cannot_be_cancelled(self):
        result = self.chat(f"取消订单 {self.by_status['shipped']}")
        self.assertEqual(result["status"], "invalid_state")

    def test_return_application_is_not_refund(self):
        identifier = self.prepare("我要退货", "delivered")
        result = self.confirm(identifier)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "submitted")
        self.assertIn("不代表退款成功", result.json()["message"])
        with self.store.session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AfterSale)), 1)
        self.assertEqual(self.confirm(identifier).json(), result.json())

    def test_expired_custom_missing_policy_go_to_human(self):
        identifier = self.by_status["delivered"]
        for mutation in [{"signed_at": time.time() - 8 * 86400}, {"product_id": "P300"}, {"policy_version": "MISSING"}]:
            self.mutate(identifier, **mutation)
            self.assertEqual(self.chat(f"{identifier} 能退货吗")["status"], "handoff_required")

    def test_missing_timestamp_requires_clarification(self):
        self.mutate(self.by_status["delivered"], signed_at=None)
        self.assertEqual(self.chat(f"{self.by_status['delivered']} 能退货吗")["status"], "clarification_required")

    def test_policy_scope_is_checked(self):
        identifier = self.by_status["delivered"]
        with self.store.session.begin() as db:
            order = db.get(Order, identifier)
            order.details = {**order.details, "channel": "other"}
        self.assertEqual(self.chat(f"{identifier} 能退货吗")["status"], "handoff_required")

    def test_duplicate_policy_versions_rejected(self):
        path = Path(self.temp.name) / "policies.json"
        policy = POLICIES["RETURN-DEMO-v1"]
        path.write_text(json.dumps([policy, policy]), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_policies(path)

    def test_concurrent_confirmation_single_write(self):
        identifier = self.prepare("我要退货", "delivered")
        def confirm():
            try:
                return self.app.state.service.confirm(identifier, *self.actor)
            except BusinessError as error:
                return {"error": error.code}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: confirm(), range(2)))
        self.assertTrue(any(r.get("status") == "submitted" for r in results))
        with self.store.session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AfterSale)), 1)

    def test_two_confirmations_cannot_repeat_business_operation(self):
        first = self.prepare("我要退货", "delivered")
        second = self.prepare("我要退货", "delivered")
        self.assertEqual(self.confirm(first).status_code, 200)
        self.assertEqual(self.confirm(second).status_code, 409)

    def test_logout_revokes_access(self):
        self.client.post("/auth/logout", headers=self.headers)
        self.assertEqual(self.client.get("/orders", headers=self.headers).status_code, 401)

    def test_preferences_are_owned_and_used(self):
        body = {"favorite_categories": ["clothing"], "answer_style": "concise"}
        self.assertEqual(self.client.put("/preferences", json=body, headers=self.headers).status_code, 200)
        self.assertNotEqual(self.client.get("/preferences", headers=self.other).json(), body)
        self.assertIn("T 恤", self.chat("推荐商品")["answer"])

    def test_mcp_envelope_tampering_and_expiry_rejected(self):
        key = "x" * 32
        envelope = sign_request(key, self.actor[1], "query_order", {"order_id": self.by_status["paid"]})
        self.assertEqual(verify_request(key, envelope)["name"], "query_order")
        envelope["arguments"]["order_id"] = "OOTHER"
        with self.assertRaises(PermissionError):
            verify_request(key, envelope)
        envelope = sign_request(key, self.actor[1], "list_orders", {})
        with patch("app.commerce.tools.time.time", return_value=time.time() + 90):
            with self.assertRaises(PermissionError):
                verify_request(key, envelope)

    def test_mcp_cannot_expose_confirmation_write_tool(self):
        envelope = sign_request("x" * 32, self.actor[1], "confirm_operation", {"confirmation_id": "x" * 32})
        with self.assertRaises(PermissionError):
            verify_request("x" * 32, envelope)

    def test_model_failure_uses_grounded_fallback(self):
        self.settings.llm_enabled = True
        async def broken(*args):
            raise ValueError("invalid structured reply")
        with patch.object(self.app.state.agent, "react_answer", broken):
            result = self.chat("P100 耳机续航多久")
        self.assertEqual(result["status"], "model_fallback")
        self.assertIn("6 小时", result["answer"])

    def test_tool_timeout_retries_are_bounded(self):
        with patch.object(self.app.state.service, "invoke", side_effect=TimeoutError) as mock:
            result = self.chat("P100 耳机续航多久")
        self.assertEqual(result["status"], "tool_unavailable")
        # RAG 超时后回退关键词检索，但每个工具的尝试次数仍以 2 次为上限。
        names = [call.args[0] for call in mock.call_args_list]
        self.assertEqual(names.count("rag_search"), 2)
        self.assertEqual(names.count("search_knowledge"), 2)

    def test_sensitive_user_text_is_redacted_before_trace(self):
        result = self.chat("P100 耳机，我的电话13800138000，地址：演示路100号")
        self.assertNotIn("13800138000", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("演示路100号", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
