import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.commerce.config import Settings
from app.commerce.intent import IntentRecognizer, IntentResult, INTENTS, parse_quantity
from pydantic import ValidationError
import test_commerce as base


class RecognizerTests(unittest.IsolatedAsyncioTestCase):
    def test_quantity_parser_handles_chinese_numerals_and_user_correction(self):
        self.assertEqual(parse_quantity("P100 买三件有货吗"), 3)
        self.assertEqual(parse_quantity("P100 买 3 件，不对，改成 5 件"), 5)
        self.assertIsNone(parse_quantity("P100 库存够吗"))

    def recognizer(self, output):
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=output)))
        return IntentRecognizer(Settings(intent_enabled=True), model), model

    async def test_fast_rules_skip_model_and_complex_negation_calls_it(self):
        recognizer, model = self.recognizer('{"intent":"after_sale_query","slots":{},"needs_clarification":false}')
        result = await recognizer.recognize("我的订单", {})
        self.assertEqual(result.source, "rule")
        model.ainvoke.assert_not_awaited()
        result = await recognizer.recognize("先别退了，我就想知道之前那个申请处理得怎么样", {})
        self.assertEqual(result.result.intent, "after_sale_query")
        self.assertEqual(result.source, "model")
        model.ainvoke.assert_awaited_once()

    async def test_contract_whitelist_and_invalid_outputs_fail_to_clarification(self):
        invalid = ['[]', 'null', 'oops', '{"intent":"delete_orders"}',
            '{"intent":"cancel_order","slots":{"user_id":"someone"}}',
            '{"intent":"rule_query","slots":{"quantity":"3"}}',
            '{"intent":"cancel_order","needs_clarification":"false"}',
            '{"intent":"order_query","slots":{"order_id":"ODEMO999999"}}']
        for output in invalid:
            recognizer, _ = self.recognizer(output)
            result = await recognizer.recognize("帮我看看这件事如何处理", {})
            self.assertEqual(result.source, "model_fallback", output)
            self.assertTrue(result.result.needs_clarification)
            self.assertEqual(result.result.intent, "other")

    async def test_timeout_no_write_and_offline_mode(self):
        recognizer, model = self.recognizer("")
        model.ainvoke.side_effect = TimeoutError()
        result = await recognizer.recognize("麻烦替我取消之前那个", {})
        self.assertTrue(result.result.needs_clarification)
        self.assertEqual(result.result.intent, "other")
        recognizer.settings.intent_enabled = False
        result = await recognizer.recognize("耳机续航多久", {})
        self.assertEqual(result.source, "rule_fallback")
        self.assertEqual(result.result.intent, "product_consult")

    async def test_api_failure_falls_back_to_local_intent_without_losing_read_request(self):
        recognizer, model = self.recognizer("")
        model.ainvoke.side_effect = OSError("simulated connection failure")
        result = await recognizer.recognize("P100 耳机怎么样，但是也想问它是否支持降噪", {})
        self.assertEqual(result.source, "model_fallback")
        self.assertEqual(result.result.intent, "product_consult")
        self.assertFalse(result.result.needs_clarification)

    async def test_context_privacy_slots_and_examples_cover_ten_intents(self):
        recognizer, model = self.recognizer('```json\n{"intent":"after_sale_query","slots":{"order_id":"ODEMO001009"}}\n```')
        result = await recognizer.recognize("那它审核过了吗，但是我不太确定 13800138000", {
            "intent": "after_sale_query", "order_id": "ODEMO001009", "session_hash": "SECRET",
            "messages": [{"content": "PRIVATE"}]})
        self.assertEqual(result.result.slots.order_id, "ODEMO001009")
        messages = model.ainvoke.call_args.args[0]
        content = json.dumps(messages)
        for private in ["SECRET", "PRIVATE", "13800138000"]:
            self.assertNotIn(private, content)
        self.assertEqual({e["output"]["intent"] for e in recognizer.examples}, INTENTS)


class SemanticGraphTests(unittest.TestCase):
    setUp = base.CommerceTests.setUp
    tearDown = base.CommerceTests.tearDown
    account = base.CommerceTests.account
    chat = base.CommerceTests.chat

    def fake_model(self, payload):
        recognizer = self.app.state.agent.recognizer
        recognizer.settings.intent_enabled = True
        recognizer.model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(payload))))

    def test_semantic_rule_topic_and_chinese_quantity_reach_tool(self):
        self.fake_model({"intent": "rule_query", "slots": {"product_id": "P100", "quantity": 3,
            "region": "remote", "rule_topic": "shipping_fee"}})
        result = self.chat("P100 买三件送新疆得多少运费")
        self.assertEqual(result["intent_source"], "rule")
        self.app.state.agent.recognizer.model.ainvoke.assert_not_awaited()
        self.assertEqual(result["data"]["facts"]["shipping_fee_cents"], 2000)
        self.assertEqual(result["tool_trace"][0]["arguments"]["quantity"], 3)

    def test_model_failure_and_multiple_intents_do_not_call_tools(self):
        for payload in [{"intent": "illegal"}, {"intent": "other", "needs_clarification": True}]:
            self.fake_model(payload)
            result = self.chat("先看看然后再帮我处理这个事情")
            self.assertEqual(result["status"], "clarification_required")
            self.assertEqual(result["tool_trace"], [])

    def test_api_timeout_fallback_preserves_order_parameter_and_ownership_guard(self):
        recognizer = self.app.state.agent.recognizer
        recognizer.settings.intent_enabled = True
        recognizer.model = SimpleNamespace(ainvoke=AsyncMock(side_effect=TimeoutError()))
        order_id = self.by_status["shipped"]
        result = self.chat(f"我不想办理售后，但是想查一下订单 {order_id} 的物流到哪里了")
        self.assertEqual(result["intent"], "logistics_query")
        self.assertEqual(result["intent_source"], "model_fallback")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tool_trace"][0]["arguments"]["order_id"], order_id)

        foreign = self.client.get("/orders", headers=self.other).json()["orders"][0]["order_id"]
        denied = self.chat(f"查看订单 {foreign}")
        self.assertEqual(denied["intent"], "order_query")
        self.assertEqual(denied["status"], "order_access_denied")
        self.assertNotIn("order", denied["data"])

    def test_model_cannot_bypass_order_ownership(self):
        foreign = self.client.get("/orders", headers=self.other).json()["orders"][0]["order_id"]
        self.fake_model({"intent": "order_query", "slots": {"order_id": foreign}})
        result = self.chat(f"麻烦帮我看看 {foreign} 的情况")
        self.assertEqual(result["status"], "order_access_denied")
