from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchArgs(Arguments):
    query: str = Field(min_length=1, max_length=1500)
    product_id: str | None = Field(default=None, pattern=r"^P[0-9]{3}$")


class RAGSearchArgs(SearchArgs):
    history: list | None = Field(default=None, max_length=8,
                                 description="最近几轮脱敏对话 [{\"role\":\"user|assistant\",\"content\":\"...\"}]，用于历史压缩与问题改写")


class OrderArgs(Arguments):
    order_id: str = Field(pattern=r"^O[A-Z0-9]{3,40}$")


class PrepareArgs(OrderArgs):
    action: Literal["cancel_order", "create_after_sale"]


class AfterSaleQueryArgs(Arguments):
    order_id: str | None = Field(default=None, pattern=r"^O[A-Z0-9]{3,40}$")
    after_sale_id: str | None = Field(default=None, pattern=r"^[Aa][Ss][A-Za-z0-9]{3,40}$")

    @model_validator(mode="after")
    def require_identifier(self):
        if not self.order_id and not self.after_sale_id:
            raise ValueError("An order or after-sale identifier is required")
        return self


class RuleArgs(Arguments):
    topic: Literal["purchase", "coupon", "shipping_fee", "dispatch", "address_change", "cancellation", "invoice", "price_protection", "logistics_delay"]
    order_id: str | None = Field(default=None, pattern=r"^O[A-Z0-9]{3,40}$")
    product_id: str | None = Field(default=None, pattern=r"^P[0-9]{3}$")
    quantity: int | None = Field(default=None, ge=1, le=999, strict=True)
    region: Literal["standard", "remote", "unsupported"] | None = None


class HandoffArgs(Arguments):
    pass


SCHEMAS = {"search_knowledge": SearchArgs, "rag_search": RAGSearchArgs, "list_orders": Arguments, "check_business_rule": RuleArgs,
           "query_after_sale": AfterSaleQueryArgs,
           "query_order": OrderArgs, "query_logistics": OrderArgs, "check_after_sales": OrderArgs,
           "prepare_operation": PrepareArgs, "human_handoff": HandoffArgs}
READ_TOOLS = {"search_knowledge", "rag_search", "list_orders", "query_order", "query_logistics", "check_after_sales", "check_business_rule", "query_after_sale"}


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign_request(key, session_hash, name, arguments, request_id="mcp"):
    payload = {"session_hash": session_hash, "name": name, "arguments": arguments,
               "expires": time.time() + 30, "request_id": request_id}
    return {**payload, "signature": hmac.new(key.encode(), canonical(payload).encode(), hashlib.sha256).hexdigest()}


def verify_request(key, envelope):
    if len(key) < 32:
        raise PermissionError("MCP signing key is not configured")
    payload = {k: v for k, v in envelope.items() if k != "signature"}
    expected = hmac.new(key.encode(), canonical(payload).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(envelope.get("signature", ""))):
        raise PermissionError("Invalid MCP signature")
    if not time.time() < float(payload.get("expires", 0)) <= time.time() + 60:
        raise PermissionError("Expired MCP request")
    name = payload["name"]
    if name not in SCHEMAS:
        raise PermissionError("Tool is not exposed over MCP")
    payload["arguments"] = SCHEMAS[name].model_validate(payload["arguments"]).model_dump()
    return payload


class ToolExecutor:
    def __init__(self, settings, service):
        self.settings, self.service = settings, service

    async def call(self, name, arguments, actor, trace, request_id):
        from app.commerce.service import BusinessError
        if name not in SCHEMAS:
            raise BusinessError("unknown_tool", "不支持的工具")
        arguments = SCHEMAS[name].model_validate(arguments).model_dump()
        started = time.perf_counter()
        event = {"tool": name, "arguments": arguments, "success": False}
        trace.append(event)
        try:
            for attempt in range(2 if name in READ_TOOLS else 1):
                try:
                    if self.settings.tool_transport == "local":
                        result = await asyncio.wait_for(asyncio.to_thread(self.service.invoke, name, arguments, *actor), timeout=15)
                    else:
                        try:
                            result = await asyncio.wait_for(self._remote(name, arguments, actor, request_id), timeout=15)
                        except BusinessError:
                            raise
                        except Exception as exc:
                            raise ConnectionError("MCP connection or response unavailable") from exc
                    break
                except (TimeoutError, ConnectionError, OSError):
                    if attempt >= (1 if name in READ_TOOLS else 0):
                        raise BusinessError("tool_unavailable", "查询服务暂时不可用，请稍后重试或转人工。")
                    await asyncio.sleep(0.15)
            event["success"] = True
            event["status"] = result.get("status", "ok")
            return result
        finally:
            event["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            import logging
            logging.getLogger(__name__).info("request_id=%s tool=%s success=%s duration_ms=%s", request_id, name, event["success"], event["duration_ms"])

    async def _remote(self, name, arguments, actor, request_id):
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        envelope = sign_request(self.settings.signing_key, actor[1], name, arguments, request_id)
        async with sse_client(self.settings.mcp_url, timeout=10) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                response = await client.call_tool("commerce_dispatch", {"envelope": envelope})
                if response.isError:
                    raise ConnectionError("MCP service rejected the request")
                result = json.loads(next(c.text for c in response.content if c.type == "text"))
                if result.get("error"):
                    from app.commerce.service import BusinessError
                    raise BusinessError(result["error"], result["message"])
                return result
