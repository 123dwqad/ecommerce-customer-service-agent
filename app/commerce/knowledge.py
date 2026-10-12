"""Demo catalog and versioned merchant rules, not a statement of statutory rights."""
from __future__ import annotations

import json
import re
from pathlib import Path
from app.commerce.rules import RULES

PRODUCTS = {
    "P100": {"product_id": "P100", "name": "轻享无线耳机", "price": "199.00", "price_cents": 19900, "stock": 12, "category": "electronics", "customized": False,
             "description": "蓝牙 5.3，单次续航 6 小时，USB-C 充电，支持通话，不支持主动降噪。", "keywords": ["耳机", "蓝牙", "续航", "降噪", "P100"]},
    "P200": {"product_id": "P200", "name": "纯棉基础 T 恤", "price": "79.00", "price_cents": 7900, "stock": 30, "category": "clothing", "customized": False,
             "description": "100% 棉，尺码 S/M/L/XL，黑白两色，建议冷水轻柔洗涤。", "keywords": ["衣服", "T恤", "T 恤", "纯棉", "尺码", "P200"]},
    "P300": {"product_id": "P300", "name": "定制刻字杯", "price": "99.00", "price_cents": 9900, "stock": 3, "category": "custom", "customized": True,
             "description": "陶瓷杯 350ml，支持定制刻字。定制商品不适用本演示店铺的无理由退货政策，质量问题转人工核实。", "keywords": ["杯", "定制", "刻字", "P300"]},
}
from app.commerce.catalog_seed import extend_catalog
extend_catalog(PRODUCTS)

def load_policies(path: Path | None = None):
    """Fail closed on ambiguous versions; policy edits require a service restart."""
    from datetime import datetime
    rows = json.loads((path or Path(__file__).with_name("policies.json")).read_text(encoding="utf-8"))
    policies = {}
    for row in rows:
        required = {"version", "days", "title", "source", "text", "merchant_id", "channel", "effective_from", "categories"}
        if not required <= row.keys() or row["version"] in policies:
            raise ValueError("Missing policy fields or conflicting policy versions")
        if type(row["days"]) is not int or not 0 < row["days"] <= 365:
            raise ValueError("Invalid policy window")
        datetime.fromisoformat(row["effective_from"])
        policies[row["version"]] = row
    return policies


POLICIES = load_policies()


def redact(text: str) -> str:
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", str(text))
    text = re.sub(r"(?<!\w)\d{17}[\dXx](?!\w)", "[证件号已隐藏]", text)
    # Do not send a supplied street address to a model or persist it in chat history.
    text = re.sub(r"(?:收货地址|地址|住址)\s*[:：是为][^，。\n]*", "[地址已隐藏]", text)
    return text


class Knowledge:
    def __init__(self, settings):
        self.settings = settings
        self.collection = None
        self.store = None

    def documents(self):
        products = list(PRODUCTS.values())
        if self.store:
            from sqlalchemy import select
            from app.commerce.db_models import Product
            from app.commerce.seed import product_data
            with self.store.session() as db:
                products = [product_data(db, pid) for pid in db.scalars(select(Product.id))]
                products = [p for p in products if p]
        docs = [{"id": p["product_id"], "title": p["name"], "text": f"演示售价 {p['price']} 元。{p['description']}",
                 "source": f"catalog://demo/{p['product_id']}", "version": "catalog-demo-v1",
                 "keywords": p["keywords"]} for p in products]
        docs.extend({"id": p["version"], "title": p["title"], "text": p["text"], "source": p["source"],
                     "version": p["version"], "keywords": ["退货", "退款", "售后", "政策", "规则", "七天", "7天"]} for p in POLICIES.values())
        docs.extend({"id": r["version"], "title": r["title"], "text": r["text"], "source": r["source"],
                     "version": r["version"], "keywords": r["keywords"]} for r in RULES)
        return docs

    def search(self, query: str, product_id: str | None = None) -> dict:
        docs = self.documents()
        ranked = []
        for doc in docs:
            score = sum(1 for keyword in doc["keywords"] if keyword.lower() in query.lower())
            if product_id == doc["id"]:
                score += 2
            if score:
                ranked.append((score, doc))
        backend = "local_keyword"
        unique = {}
        for _, doc in sorted(ranked, key=lambda item: item[0], reverse=True):
            unique.setdefault(doc["id"], {k: v for k, v in doc.items() if k != "keywords"})
        return {"status": "ok" if unique else "no_results", "hits": list(unique.values())[:3], "backend": backend}
