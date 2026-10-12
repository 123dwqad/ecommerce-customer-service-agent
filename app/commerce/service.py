from __future__ import annotations

import secrets
import time
from datetime import datetime

from sqlalchemy import select, update, func

from app.commerce.knowledge import POLICIES, PRODUCTS
from app.commerce.store import AfterSale, Confirmation, Handoff, Order
from app.commerce.rules import evaluate

try:
    from app.commerce.rag.service import RagNotReady
except Exception:  # RAG 依赖缺失时主服务仍可启动
    RagNotReady = RuntimeError

AFTER_SALE_STATUSES = {"submitted": "已提交，待审核", "approved": "审核通过，待退回商品",
    "returned": "已记录退回商品，待后续处理", "closed": "售后流程已关闭", "rejected": "申请未通过审核"}


class BusinessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CommerceService:
    def __init__(self, store, knowledge, rag=None):
        self.store = store
        self.knowledge = knowledge
        self.knowledge.store = store
        self._rag = rag

    def rag(self):
        """懒加载 RAG 服务：依赖缺失或未启用时返回 None，调用方回退 search_knowledge。"""
        if self._rag is None:
            try:
                from app.commerce.config import Settings
                from app.commerce.rag.service import RAGService
                settings = Settings()
                if not settings.rag_enabled:
                    return None
                self._rag = RAGService(self.store, settings)
            except Exception:
                import logging
                logging.getLogger(__name__).warning("RAG service unavailable, falling back to search_knowledge",
                                                    exc_info=True)
                self._rag = False
        return self._rag if self._rag is not False else None

    def product(self, product_id):
        from app.commerce.seed import product_data
        with self.store.session() as db:
            return product_data(db, product_id)

    def owned(self, db, user_id, order_id):
        order = db.get(Order, order_id)
        if not order or order.user_id != user_id:
            raise BusinessError("order_access_denied", "订单不存在或不属于当前账号，请核对订单号。")
        return order

    def public_order(self, order):
        # Allowlist, not a blacklist: address/phone/name never enter model context.
        return {"order_id": order.id, "product_id": order.product_id,
                "product_name": (self.product(order.product_id) or {}).get("name", "未知商品"),
                "status": order.status, "signed_at": order.signed_at, "version": order.version,
                "policy_version": order.policy_version}

    def check_return(self, order):
        policy = POLICIES.get(order.policy_version)
        product = self.product(order.product_id)
        if not policy or policy.get("conflict") or not product:
            return {"eligible": False, "status": "handoff_required", "reason": "政策缺失、冲突或商品属性不完整，需要人工核实。"}
        evidence = {"policy_version": policy["version"], "source": policy["source"], "policy_text": policy["text"]}
        if (order.details.get("merchant_id") != policy["merchant_id"]
            or order.details.get("channel") != policy["channel"]
            or product.get("category") not in policy["categories"]
            or order.details.get("purchased_at", 0) < datetime.fromisoformat(policy["effective_from"]).timestamp()):
            return {**evidence, "eligible": False, "status": "handoff_required", "reason": "订单不匹配该政策的店铺、渠道或生效范围，需要人工核实。"}
        if product.get("customized"):
            return {**evidence, "eligible": False, "status": "handoff_required", "reason": "定制商品不适用该无理由退货规则，质量问题请转人工。"}
        if order.status != "delivered":
            return {**evidence, "eligible": False, "status": "not_eligible", "reason": "当前订单未处于已签收状态。"}
        if order.signed_at is None or order.signed_at > time.time():
            return {**evidence, "eligible": False, "status": "clarification_required", "reason": "签收时间缺失或异常，需要核实。"}
        if time.time() - order.signed_at > policy["days"] * 86400:
            return {**evidence, "eligible": False, "status": "handoff_required", "reason": "已超出演示店铺无理由退货申请期限，可转人工核实其他售后诉求。"}
        return {**evidence, "eligible": True, "status": "eligible", "reason": "符合提交无理由退货申请的初步条件，后续还需核实商品完好情况；不代表退款获批。"}

    def invoke(self, name: str, args: dict, user_id: str, session_hash: str):
        # Re-check the session at the business boundary, including calls from MCP.
        actual_user, _ = self.store.actor_from_session(session_hash)
        if actual_user != user_id:
            raise PermissionError("会话身份不匹配")
        if name == "query_after_sale":
            from app.commerce.tools import AfterSaleQueryArgs
            return self.query_after_sale(AfterSaleQueryArgs.model_validate(args).model_dump(), user_id)
        if name == "search_knowledge":
            return self.knowledge.search(args["query"], args.get("product_id"))
        if name == "rag_search":
            rag = self.rag()
            if rag is None:
                raise BusinessError("rag_unavailable", "知识库检索服务不可用，请稍后重试。")
            try:
                return rag.search(query=args["query"], product_id=args.get("product_id"),
                                  history=args.get("history") or [])
            except RagNotReady:
                raise BusinessError("rag_unavailable", "知识库索引构建中，已回退关键词检索。") from None
        if name == "check_business_rule":
            with self.store.session() as db:
                order = self.owned(db, user_id, args["order_id"]) if args.get("order_id") else None
                product_id = order.product_id if order else args.get("product_id")
                if order and args.get("product_id") and order.product_id != args["product_id"]:
                    return {"status": "clarification_required", "reason": "商品编号与订单商品不一致，请核对后再查询。"}
                product = self.product(product_id)
                if product_id and not product:
                    return {"status": "clarification_required", "reason": "商品编号不存在，无法匹配规则。"}
                return {"order_id": order.id if order else None, **evaluate(args["topic"], product, order, args.get("quantity"), args.get("region"))}
        if name == "list_orders":
            with self.store.session() as db:
                return {"status": "ok", "orders": [self.public_order(o) for o in db.scalars(select(Order).where(Order.user_id == user_id).order_by(Order.id))]}
        if name == "human_handoff":
            # Persist a local queue record, never claim an external agent was contacted.
            identifier = "H" + secrets.token_hex(8)
            with self.store.session.begin() as db:
                db.add(Handoff(id=identifier, user_id=user_id, reason="customer_request"))
            return {"status": "queued", "handoff_id": identifier, "message": "已记录到演示人工队列；尚未连接真实客服平台。"}
        if name == "confirm_operation":
            return self.confirm(args["confirmation_id"], user_id, session_hash)
        with self.store.session.begin() as db:
            order = self.owned(db, user_id, args["order_id"])
            if name == "query_order":
                return {"status": "ok", "order": self.public_order(order)}
            if name == "query_logistics":
                from app.commerce.db_models import Shipment, TrackingEvent
                shipments = list(db.scalars(select(Shipment).where(Shipment.order_id == order.id)))
                if shipments:
                    events = list(db.scalars(select(TrackingEvent).where(
                        TrackingEvent.shipment_id.in_([s.id for s in shipments])).order_by(TrackingEvent.occurred_at)))
                    return {"status": "ok", "order_id": order.id,
                        "simulated": all(s.carrier.startswith("模拟") for s in shipments),
                        "shipments": [{"shipment_id": s.id, "carrier": s.carrier,
                            "tracking_number": s.tracking_number, "status": s.status} for s in shipments],
                        "events": [{"shipment_id": e.shipment_id, "status": e.status, "occurred_at": e.occurred_at,
                            "description": e.description, "location": e.location} for e in events]}
                return {"status": "ok", "order_id": order.id, "simulated": True,
                        "events": order.details.get("tracking", [])}
            if name == "check_after_sales":
                return {"order_id": order.id, **self.check_return(order)}
            if name == "prepare_operation":
                action = args["action"]
                if action not in {"cancel_order", "create_after_sale"}:
                    raise BusinessError("invalid_action", "不支持此操作")
                if action == "cancel_order":
                    check = evaluate("cancellation", self.product(order.product_id), order)
                    if check["status"] != "eligible":
                        raise BusinessError("invalid_state", check["reason"])
                if action == "create_after_sale":
                    check = self.check_return(order)
                    if not check["eligible"]:
                        return {"order_id": order.id, **check}
                    if db.scalar(select(AfterSale).where(AfterSale.order_id == order.id)):
                        raise BusinessError("already_submitted", "该订单已有售后申请，请勿重复提交。")
                identifier = secrets.token_urlsafe(24)
                db.add(Confirmation(id=identifier, session_hash=session_hash, user_id=user_id,
                    order_id=order.id, order_version=order.version, action=action, expires=time.time() + 300))
                return {"status": "confirmation_required", "confirmation_id": identifier,
                        "order_id": order.id, "product_name": self.product(order.product_id)["name"], "action": action,
                        **{k: check[k] for k in ("source", "policy_version", "policy_text", "title") if k in check},
                        "expires_in": 300, "message": "请核对订单和操作，点击确认后才会提交。"}
        raise BusinessError("unknown_tool", "不支持的工具")

    def query_after_sale(self, args, user_id):
        from app.commerce.db_models import AfterSaleDetail, AfterSaleEvent
        with self.store.session() as db:
            order = self.owned(db, user_id, args["order_id"]) if args.get("order_id") else None
            if args.get("after_sale_id"):
                sale = db.scalar(select(AfterSale).where(func.upper(AfterSale.id) == args["after_sale_id"].upper()))
                if not sale or sale.user_id != user_id:
                    raise BusinessError("after_sale_access_denied", "售后单不存在或不属于当前账号，请核对。")
                owned_order = self.owned(db, user_id, sale.order_id)
                if order and order.id != sale.order_id:
                    raise BusinessError("after_sale_order_mismatch", "售后单与订单不匹配，请核对后再查询。")
                order = owned_order
            else:
                sale = db.scalar(select(AfterSale).where(AfterSale.order_id == order.id, AfterSale.user_id == user_id))
            if not sale:
                return {"status": "no_after_sale", "order_id": order.id, "product_id": order.product_id,
                    "message": "该订单暂无已提交的售后申请。如需申请，请明确提出申请退货并完成确认。"}
            detail = db.get(AfterSaleDetail, sale.id)
            events = list(db.scalars(select(AfterSaleEvent).where(AfterSaleEvent.after_sale_id == sale.id)
                .order_by(AfterSaleEvent.occurred_at, AfterSaleEvent.id)))
            # Do not expose user text, credentials, addresses, or internal review notes.
            public = {"after_sale_id": sale.id, "status": sale.status,
                "status_label": AFTER_SALE_STATUSES.get(sale.status, "未知处理状态，需人工核实"),
                "policy_version": sale.policy_version,
                "created_at": detail.created_at if detail else None,
                "updated_at": max([e.occurred_at for e in events] + ([detail.updated_at] if detail else []), default=None),
                "events": [{"status": e.status, "status_label": AFTER_SALE_STATUSES.get(e.status, "未知处理状态"),
                    "occurred_at": e.occurred_at} for e in events]}
            if detail:
                public.update(quantity=detail.quantity, requested_cents=detail.requested_cents)
            return {"status": "ok", "order_id": order.id, "product_id": order.product_id, "after_sale": public,
                "message": "以上为演示售后记录；审核通过或流程关闭均不代表真实退款到账。"}

    def confirm(self, identifier, user_id, session_hash):
        # Claim confirmation and CAS order version inside ONE transaction.
        # Same confirmation is the idempotency key; retries return the recorded result.
        with self.store.session.begin() as db:
            confirmation = db.get(Confirmation, identifier)
            if not confirmation or confirmation.user_id != user_id or confirmation.session_hash != session_hash:
                raise BusinessError("confirmation_denied", "确认记录不存在或不属于当前会话。")
            if confirmation.consumed:
                return confirmation.result
            if confirmation.expires <= time.time():
                raise BusinessError("confirmation_expired", "确认已过期，请重新发起操作。")
            claimed = db.execute(update(Confirmation).where(Confirmation.id == identifier, Confirmation.consumed == 0).values(consumed=1))
            if claimed.rowcount != 1:
                raise BusinessError("operation_in_progress", "操作正在处理，请使用同一确认记录重试。")
            order = self.owned(db, user_id, confirmation.order_id)
            if order.version != confirmation.order_version:
                raise BusinessError("stale_confirmation", "订单状态已经变化，请重新查询并确认。")
            if confirmation.action == "cancel_order":
                check = evaluate("cancellation", self.product(order.product_id), order)
                if check["status"] != "eligible":
                    raise BusinessError("invalid_state", check["reason"])
                status = "cancelled"
                result = {"status": "completed", "action": "cancel_order", "order_id": order.id,
                          "message": "演示订单已取消；未执行真实支付退款。"}
            else:
                check = self.check_return(order)
                if not check["eligible"]:
                    raise BusinessError("eligibility_changed", "售后资格已变化，请重新查询。")
                if db.scalar(select(AfterSale).where(AfterSale.order_id == order.id)):
                    raise BusinessError("already_submitted", "该订单已有售后申请。")
                status = order.status
                after_sale_id = "AS" + secrets.token_hex(8)
                sale = AfterSale(id=after_sale_id, order_id=order.id, user_id=user_id, policy_version=order.policy_version)
                db.add(sale)
                db.flush()
                from app.commerce.seed import enrich_order, add_after_sale_detail
                enrich_order(db, order)
                add_after_sale_detail(db, sale)
                result = {"status": "submitted", "action": "create_after_sale", "order_id": order.id,
                          "after_sale_id": after_sale_id, "message": "演示售后申请已提交，待审核；不代表退款成功。"}
            changed = db.execute(update(Order).where(Order.id == order.id, Order.version == confirmation.order_version).values(status=status, version=Order.version + 1))
            if changed.rowcount != 1:
                raise BusinessError("stale_confirmation", "订单状态已经变化，请重新发起操作。")
            confirmation.result = result
            from app.commerce.db_models import OrderEvent
            db.add(OrderEvent(id=f"OP-{identifier}", order_id=order.id, status=status,
                occurred_at=time.time(), note=confirmation.action))
            return result
