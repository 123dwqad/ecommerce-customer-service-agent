"""Idempotent catalog/bootstrap and explicitly requested larger demo dataset."""
import secrets
import time
from datetime import datetime
from sqlalchemy import select, func
from app.commerce.store import User, Order, AfterSale, Handoff, password_hash
from app.commerce.db_models import (Product, SKU, PolicyRecord, OrderItem, Shipment,
    TrackingEvent, AfterSaleDetail, AfterSaleEvent, OrderEvent, SeedBatch)
from app.commerce.knowledge import PRODUCTS, POLICIES
from app.commerce.rules import RULES

BATCH = "commerce-scenarios-v1"


def demo_tracking_data(status, *, now=None, signed_at=None):
    """Build a synthetic multi-stop carrier trace for demo orders only."""
    now = now or time.time()
    if status == "shipped":
        shipped_at = now - 20 * 3600
        entries = [
            (0, "collected", "演示市发货仓", "模拟快递已揽收"),
            (1, "in_transit", "演示市始发分拨中心", "包裹离开发货地分拨中心"),
            (8, "in_transit", "演示市干线转运中心", "包裹到达干线转运中心"),
            (14, "in_transit", "演示市中转分拨中心", "包裹完成中转，发往目的地"),
            (18, "in_transit", "演示市目的地分拨中心", "包裹到达目的地分拨中心，等待派送"),
        ]
    elif status == "delivered":
        signed_at = signed_at or now - 48 * 3600
        shipped_at = signed_at - 48 * 3600
        entries = [
            (0, "collected", "演示市发货仓", "模拟快递已揽收"),
            (1, "in_transit", "演示市始发分拨中心", "包裹离开发货地分拨中心"),
            (10, "in_transit", "演示市干线转运中心", "包裹到达干线转运中心"),
            (36, "in_transit", "演示市配送站", "包裹到达配送站，开始派送"),
            (48, "delivered", "演示市演示路", "模拟收件人已签收"),
        ]
    else:
        return None
    events = [{"status": state, "occurred_at": shipped_at + hours * 3600,
               "location": location, "description": description}
              for hours, state, location, description in entries]
    return {"shipped_at": shipped_at, "tracking": events,
            "last_logistics_at": events[-1]["occurred_at"]}


def seed_catalog(db):
    for p in PRODUCTS.values():
        if not db.get(Product, p["product_id"]):
            db.add(Product(id=p["product_id"], name=p["name"], category=p["category"],
                description=p["description"], keywords=p["keywords"], customized=p["customized"],
                attributes={"demo": True, "specification_notes": p["description"]}))
            db.flush()
        for n in range(3):
            sid = f"{p['product_id']}-S{n + 1}"
            if not db.get(SKU, sid):
                specifications = ({"color": "黑色", "size": ["M", "L", "XL"][n]}
                    if p["product_id"] in {"P200", "P201", "P202", "P203"}
                    else {"color": ["黑色", "白色", "灰色"][n]})
                db.add(SKU(id=sid, product_id=p["product_id"], specifications=specifications,
                    price_cents=p["price_cents"], stock=p["stock"] // 3 + int(n < p["stock"] % 3)))
    for policy in [*POLICIES.values(), *RULES]:
        if not db.get(PolicyRecord, policy["version"]):
            db.add(PolicyRecord(version=policy["version"], topic=policy.get("topic", "return"),
                merchant_id=policy["merchant_id"], channel=policy["channel"], content=policy))
    db.flush()


def product_data(db, product_id):
    p = db.get(Product, product_id)
    if not p or p.status != "active":
        return None
    skus = list(db.scalars(select(SKU).where(SKU.product_id == product_id, SKU.status == "active")))
    if not skus:
        return None
    price = min(s.price_cents for s in skus)
    return dict(product_id=p.id, name=p.name, category=p.category, description=p.description,
        customized=p.customized, keywords=p.keywords, price_cents=price, price=f"{price / 100:.2f}",
        stock=sum(s.stock for s in skus), skus=[dict(sku_id=s.id, specifications=s.specifications,
            price_cents=s.price_cents, stock=s.stock) for s in skus])


def enrich_order(db, order):
    """Add missing child records only; do not overwrite existing facts or fabricate missing history."""
    item = db.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    d = order.details
    if not item:
        p = db.get(Product, order.product_id)
        quantity, amount, paid = d.get("quantity"), d.get("merchandise_cents"), d.get("paid_merchandise_cents")
        if not p or type(quantity) is not int or quantity < 1 or type(amount) is not int or type(paid) is not int:
            return
        if amount < 0 or paid < 0 or amount % quantity:
            return
        sku = db.get(SKU, d.get("sku_id", f"{p.id}-S1"))
        if not sku or sku.product_id != p.id:
            return
        item = OrderItem(id=f"I{order.id}", order_id=order.id, sku_id=sku.id,
            quantity=quantity, unit_price_cents=amount // quantity, paid_cents=paid,
            policy_version=order.policy_version, product_snapshot={"product_id": p.id, "name": p.name,
                "category": p.category, "customized": p.customized, "specifications": sku.specifications})
        db.add(item)
        db.flush()
    if not db.get(OrderEvent, f"E{order.id}-snapshot"):
        db.add(OrderEvent(id=f"E{order.id}-snapshot", order_id=order.id, status=order.status,
            occurred_at=time.time(), note="初始化时的订单状态快照；不代表该状态的实际发生时间"))
    if order.status not in {"shipped", "delivered"}:
        return
    if db.scalar(select(Shipment).where(Shipment.order_id == order.id)):
        return
    events = d.get("tracking", [])
    shipped = d.get("shipped_at")
    # Legacy records have no precise shipment time; leave them on the legacy read path.
    if not isinstance(shipped, (float, int)):
        return
    shipment = Shipment(id=f"S{order.id}", order_id=order.id, carrier="模拟快递",
        tracking_number=f"DEMO-{order.id}", status=order.status, shipped_at=shipped, signed_at=order.signed_at)
    db.add(shipment)
    db.flush()
    for n, e in enumerate(events):
        if not isinstance(e.get("occurred_at"), (int, float)):
            continue
        db.add(TrackingEvent(id=f"T{order.id}-{n}", shipment_id=shipment.id,
            occurred_at=e["occurred_at"], status=e["status"], location=e.get("location", "模拟分拨中心"),
            description=e["description"]))


def add_after_sale_detail(db, sale, reason="用户申请无理由退货", created_at=None):
    if db.get(AfterSaleDetail, sale.id):
        return
    item = db.scalar(select(OrderItem).where(OrderItem.order_id == sale.order_id))
    if not item:
        return
    at = created_at or time.time()
    db.add(AfterSaleDetail(after_sale_id=sale.id, order_item_id=item.id, quantity=item.quantity,
        reason=reason, requested_cents=item.paid_cents, created_at=at, updated_at=at))
    db.add(AfterSaleEvent(id=f"{sale.id}-submitted", after_sale_id=sale.id, status="submitted",
        occurred_at=at, note="演示退货申请已提交；未执行真实退款"))


def seed_scenarios(store, credentials):
    now = time.time()
    with store.session.begin() as db:
        if db.get(SeedBatch, BATCH):
            return False
        seed_catalog(db)
        for existing in db.scalars(select(Order)):
            enrich_order(db, existing)
        for sale in db.scalars(select(AfterSale)):
            add_after_sale_detail(db, sale)
        ordinary = [p for p in PRODUCTS.values() if not p["customized"]]
        for u, account in enumerate(credentials):
            uid = account["user_id"]
            if db.get(User, uid):
                raise ValueError(f"Refusing to reuse existing account: {uid}")
            salt = secrets.token_hex(16)
            db.add(User(id=uid, salt=salt, password=password_hash(account["password"], salt),
                preferences={"favorite_categories": ["clothing" if u % 2 else "electronics"], "answer_style": "concise"}))
            db.flush()
            for n in range(12):
                oid = f"ODEMO{u + 1:03}{n + 1:03}"
                p = PRODUCTS["P300"] if n == 3 else ordinary[(u * 3 + n) % len(ordinary)]
                status = ["pending_payment", "paid", "paid", "paid", "shipped", "shipped",
                          "delivered", "delivered", "delivered", "delivered", "delivered", "cancelled"][n]
                # Some final scenarios show a completed/rejected after-sale instead of cancellation.
                if n == 11 and u % 2:
                    status = "delivered"
                age_days = 18 if n == 7 else 6
                purchased = now - age_days * 86400
                paid_at = None if n == 0 else (now - 3600 if n == 1 else purchased + 1800)
                signed = now - (10 if n == 7 else 2) * 86400 if status == "delivered" else None
                shipped = purchased + 86400 if status in {"shipped", "delivered"} else None
                last = signed if signed else (now - (72 if n == 5 else 6) * 3600 if shipped else None)
                qty = n % 3 + 1
                amount = p["price_cents"] * qty
                discount = 2000 if amount >= 20000 and not p["customized"] and n % 2 == 0 else 0
                tracking = []
                if shipped:
                    tracking = [dict(status="collected", occurred_at=shipped, location="模拟发货仓", description="模拟快递已揽收"),
                        dict(status="in_transit", occurred_at=shipped + 7200, location="模拟始发分拨中心", description="包裹离开始发分拨中心"),
                        dict(status="delivered" if signed else "in_transit", occurred_at=last,
                            location="模拟目的地分拨中心", description="包裹已签收" if signed else "包裹运输中")]
                rules = {r["topic"]: r["version"] for r in RULES if p["category"] in r["categories"]
                    and datetime.fromisoformat(r["effective_from"]).timestamp() <= purchased}
                order = Order(id=oid, user_id=uid, product_id=p["product_id"], status=status,
                    signed_at=signed, policy_version="RETURN-DEMO-v1", details={
                    "seed_batch": BATCH, "scenario": ["unpaid", "awaiting_dispatch", "dispatch_overdue", "custom_in_production",
                        "in_transit", "logistics_stalled", "return_eligible", "return_expired", "after_sale_submitted",
                        "after_sale_approved", "after_sale_returned", "cancelled_or_reviewed"][n],
                    "merchant_id": "demo", "channel": "web", "purchased_at": purchased, "paid_at": paid_at,
                    "shipped_at": shipped, "quantity": qty, "merchandise_cents": amount,
                    "paid_merchandise_cents": 0 if n == 0 else amount - discount, "discount_cents": discount,
                    "sku_id": f"{p['product_id']}-S{n % 3 + 1}", "rule_versions": rules,
                    "shipping_region": "remote" if u % 3 == 0 else "standard",
                    "invoice_status": "issued" if n == 6 else "not_requested", "production_started": n == 3,
                    "last_logistics_at": last, "tracking": tracking})
                db.add(order)
                db.flush()
                enrich_order(db, order)
                if n >= 8 and status == "delivered":
                    final = {8: "submitted", 9: "approved", 10: "returned", 11: "closed" if u % 4 == 1 else "rejected"}[n]
                    sale = AfterSale(id=f"ASDEMO{u + 1:03}{n:03}", order_id=oid, user_id=uid,
                        status=final, policy_version=order.policy_version)
                    db.add(sale)
                    db.flush()
                    created = signed + 3600
                    add_after_sale_detail(db, sale, reason="演示申请：尺码或使用体验不合适", created_at=created)
                    steps = {"submitted": [], "approved": ["approved"], "returned": ["approved", "returned"],
                        "closed": ["approved", "returned", "closed"], "rejected": ["rejected"]}[final]
                    for j, step in enumerate(steps, 1):
                        db.add(AfterSaleEvent(id=f"{sale.id}-{step}", after_sale_id=sale.id, status=step,
                            occurred_at=created + j * 3600, note="模拟售后阶段；不代表真实退货或资金流转"))
                    db.flush()
                    db.get(AfterSaleDetail, sale.id).updated_at = created + len(steps) * 3600
            db.add(Handoff(id=f"HDEMO{u + 1:03}", user_id=uid, reason="demo_logistics_delay"))
        db.add(SeedBatch(id=BATCH, summary={"users": len(credentials), "orders": len(credentials) * 12}))
    return True


def table_counts(store):
    from app.commerce.store import Base
    with store.session() as db:
        return {table.name: db.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables}
