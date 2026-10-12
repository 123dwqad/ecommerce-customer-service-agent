"""Versioned merchant rules. All money is integer cents; no LLM decisions or writes."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

TOPICS = ("purchase", "coupon", "shipping_fee", "dispatch", "address_change", "cancellation", "invoice", "price_protection", "logistics_delay")


def load_rules(path=None):
    rows = json.loads((path or Path(__file__).with_name("business_rules.json")).read_text(encoding="utf-8"))
    versions = set()
    for row in rows:
        required = {"topic", "version", "title", "merchant_id", "channel", "effective_from", "categories", "keywords", "parameters", "text"}
        if not required <= row.keys() or row["topic"] not in TOPICS or row["version"] in versions:
            raise ValueError("Invalid or duplicate business rule")
        start = datetime.fromisoformat(row["effective_from"])
        if start.tzinfo is None:
            raise ValueError("Policy timestamps must include timezone")
        if row.get("effective_to") and datetime.fromisoformat(row["effective_to"]) <= start:
            raise ValueError("Invalid rule effective period")
        expected = {
            "purchase": {"max_quantity": int},
            "coupon": {"threshold_cents": int, "discount_cents": int, "excluded_categories": list, "stackable": bool},
            "shipping_fee": {"free_threshold_cents": int, "standard_fee_cents": int, "remote_fee_cents": int},
            "dispatch": {"standard_hours": int, "custom_hours": int},
            "address_change": {"allowed_states": list},
            "cancellation": {"allowed_states": list, "block_custom_production": bool},
            "invoice": {"allowed_states": list},
            "price_protection": {"hours": int, "excluded_categories": list, "allowed_states": list},
            "logistics_delay": {"stale_hours": int},
        }[row["topic"]]
        for key, kind in expected.items():
            value = row["parameters"].get(key)
            if type(value) is not kind or (kind is int and value < 0) or (kind is list and not all(isinstance(v, str) for v in value)):
                raise ValueError(f"Invalid parameter {key} in {row['version']}")
            if kind is int and ("hours" in key or key == "max_quantity") and value == 0:
                raise ValueError(f"Parameter {key} must be positive")
        if row["topic"] == "coupon" and row["parameters"]["discount_cents"] > row["parameters"]["threshold_cents"]:
            raise ValueError("Discount must not exceed threshold")
        versions.add(row["version"])
        row["source"] = f"merchant://demo/rules/{row['version']}"
    # Ambiguous historical matching must fail before serving any query.
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if (a["topic"], a["merchant_id"], a["channel"]) != (b["topic"], b["merchant_id"], b["channel"]):
                continue
            if not set(a["categories"]) & set(b["categories"]):
                continue
            start = max(datetime.fromisoformat(a["effective_from"]).timestamp(), datetime.fromisoformat(b["effective_from"]).timestamp())
            end = min(datetime.fromisoformat(r["effective_to"]).timestamp() if r.get("effective_to") else float("inf") for r in (a, b))
            if start < end:
                raise ValueError("Overlapping business rule versions")
    return rows


RULES = load_rules()


def match_topics(query):
    return [r["topic"] for r in RULES if any(k in query for k in r["keywords"])]


def money(cents):
    return f"{cents / 100:.2f}"


def evaluate(topic, product=None, order=None, quantity=None, region=None):
    now = time.time()
    details = order.details if order else {}
    at = details.get("purchased_at") if order else now
    if not isinstance(at, (float, int)) or at > now:
        return {"status": "clarification_required", "reason": "订单购买时间缺失或异常，需要先核实。"}
    pinned = details.get("rule_versions", {}).get(topic)
    candidates = [r for r in RULES if r["topic"] == topic
        and (not pinned or r["version"] == pinned)
        and r["merchant_id"] == details.get("merchant_id", "demo" if not order else None)
        and r["channel"] == details.get("channel", "web" if not order else None)
        and (not product or product["category"] in r["categories"])
        and datetime.fromisoformat(r["effective_from"]).timestamp() <= at
        and (not r.get("effective_to") or at < datetime.fromisoformat(r["effective_to"]).timestamp())]
    if len(candidates) != 1:
        return {"status": "handoff_required", "reason": "未找到唯一适用的规则版本，请转人工核实。"}
    rule = candidates[0]
    evidence = {"topic": topic, "policy_version": rule["version"], "source": rule["source"],
                "title": rule["title"], "policy_text": rule["text"]}
    def result(status, reason, **facts):
        return {**evidence, "status": status, "reason": reason, "facts": facts}
    def missing(reason):
        return result("clarification_required", reason)
    p = rule["parameters"]
    if not order and not product:
        return result("policy_info", rule["text"])
    if topic in {"dispatch", "address_change", "cancellation", "invoice", "price_protection", "logistics_delay"} and not order:
        return result("policy_info", rule["text"] + " 请提供订单号以核对这笔订单。")
    if not product:
        return missing("缺少商品属性，无法判断，请核实商品编号。")
    if topic == "purchase":
        if quantity is None:
            return missing("请说明购买数量（例如“P100 买 2 件，有货吗”）。")
        stock = product.get("stock")
        if not isinstance(stock, int):
            return missing("缺少库存数据，需要核实。")
        allowed = min(stock, p["max_quantity"])
        return result("eligible" if quantity <= allowed else "not_eligible",
            f"该商品演示库存 {stock} 件，单笔限购 {p['max_quantity']} 件；本次 {quantity} 件" + ("可购买，但尚未锁库存。" if quantity <= allowed else "超出可购买数量。"), quantity=quantity, stock=stock, max_quantity=p["max_quantity"])
    if topic in {"coupon", "shipping_fee"}:
        if topic == "coupon" and product["category"] in p["excluded_categories"]:
            return result("not_eligible", "该商品不参与本演示店铺的优惠活动。")
        if order:
            amount = details.get("merchandise_cents" if topic == "coupon" else "paid_merchandise_cents")
        else:
            if quantity is None:
                return missing("请说明商品和数量，才能试算金额；例如“P100 买 2 件能用优惠吗”。")
            amount = product["price_cents"] * quantity
        if type(amount) is not int or amount < 0:
            return missing("缺少可靠的订单商品金额，不能用当前商品价格替代历史实付金额。")
        if topic == "coupon":
            if order and type(details.get("discount_cents")) is not int:
                return missing("缺少订单已用优惠信息，不能判断叠加资格。")
            if product["category"] in p["excluded_categories"] or (not p["stackable"] and details.get("discount_cents", 0) > 0):
                return result("not_eligible", "该商品不参与优惠，或订单已有优惠不可叠加。", merchandise_cents=amount)
            discount = p["discount_cents"] if amount >= p["threshold_cents"] else 0
            return result("eligible" if discount else "not_eligible", f"商品金额 {money(amount)} 元，门槛 {money(p['threshold_cents'])} 元；可试算优惠 {money(discount)} 元。未发券、未修改订单。", merchandise_cents=amount, discount_cents=discount)
        actual_region = details.get("shipping_region") if order else region
        if order and region and region != actual_region:
            return missing("你提供的区域与订单记录不一致，请先核实；不会用聊天区域替换订单记录。")
        if actual_region not in {"standard", "remote", "unsupported"}:
            return missing("请说明普通区域、新疆/西藏或港澳台/海外，无需详细地址。")
        if actual_region == "unsupported":
            return result("not_eligible", "演示店铺暂不配送到港澳台及海外。")
        fee = p["remote_fee_cents"] if actual_region == "remote" else (0 if amount >= p["free_threshold_cents"] else p["standard_fee_cents"])
        return result("eligible", f"按商品实付参考金额 {money(amount)} 元试算，运费 {money(fee)} 元；未修改运费或创建订单。", shipping_fee_cents=fee, merchandise_cents=amount, region=actual_region)
    if topic in {"address_change", "cancellation"}:
        if order.status not in p["allowed_states"]:
            return result("not_eligible", "当前订单状态不允许直接" + ("取消" if topic == "cancellation" else "修改收货信息") + "，可转人工核实。")
        if topic == "cancellation" and p["block_custom_production"] and product["customized"]:
            if "production_started" not in details:
                return missing("缺少定制商品制作状态，需要核实后才能取消。")
            if details["production_started"]:
                return result("not_eligible", "定制商品已开始制作，不能直接取消，可转人工核实。")
        return result("eligible", "符合申请条件。" + ("仍需点击订单确认卡片后才会取消。" if topic == "cancellation" else "本对话不执行地址修改，请前往订单页或人工渠道办理。"))
    if topic == "invoice":
        if order.status not in p["allowed_states"]:
            return result("not_eligible", "订单未付款或已取消，不符合本演示店铺的开票条件。")
        if details.get("invoice_status") == "issued":
            return result("not_eligible", "该订单已有发票，不重复开具；抬头更正或重开请转人工。")
        if details.get("invoice_status") != "not_requested" or type(details.get("paid_merchandise_cents")) is not int or details["paid_merchandise_cents"] < 0:
            return missing("缺少开票状态或商品实付金额，需要核实。")
        amount = details["paid_merchandise_cents"]
        return result("eligible", f"可申请电子普通发票，参考金额 {money(amount)} 元；尚未开票。", invoice_amount_cents=amount)
    if topic in {"dispatch", "price_protection"}:
        if order.status not in (p["allowed_states"] if topic == "price_protection" else {"paid", "shipped", "delivered"}):
            return result("not_eligible", "订单未付款或已取消，不适用付款后的时效规则。")
        paid_at = details.get("paid_at")
        if not isinstance(paid_at, (int, float)) or paid_at > now:
            return missing("付款时间缺失或异常，需要核实。")
        if topic == "dispatch":
            if order.status != "paid":
                return result("ok", "订单已经发货或签收，可以继续查询物流轨迹。")
            hours = p["custom_hours"] if product["customized"] else p["standard_hours"]
            deadline = paid_at + hours * 3600
            return result("handoff_required" if now > deadline else "ok", f"适用付款后 {hours} 小时发货规则；" + ("已超时未发货，建议转人工核实。" if now > deadline else "目前仍在发货时限内，不承诺到达日期。"), deadline=datetime.fromtimestamp(deadline).astimezone().isoformat())
        if product["category"] in p["excluded_categories"] or now - paid_at > p["hours"] * 3600:
            return result("not_eligible", "商品不参与保价，或已经超过保价期限。")
        quantity = details.get("quantity")
        amount = details.get("paid_merchandise_cents")
        if type(quantity) is not int or quantity <= 0 or type(amount) is not int or amount < 0:
            return missing("缺少购买数量或商品实付金额，需要核实。")
        difference = max(0, amount - product["price_cents"] * quantity)
        return result("eligible" if difference else "not_eligible", f"与同一商品当前演示价格比较，参考差额 {money(difference)} 元；未提交保价申请或退款。", difference_cents=difference)
    if topic == "logistics_delay":
        if order.status == "delivered":
            return result("handoff_required", "订单记录已签收；若实际未收到，请转人工核实签收情况，不自动判定丢件。")
        if order.status != "shipped":
            return result("not_eligible", "订单未处于运输中，请先查询订单状态或发货时效。")
        updated = details.get("last_logistics_at")
        if not isinstance(updated, (int, float)) or updated > now:
            return missing("缺少可靠的物流更新时间，需要核实。")
        stale = now - updated >= p["stale_hours"] * 3600
        return result("handoff_required" if stale else "ok", f"物流停滞核实门槛为 {p['stale_hours']} 小时；" + ("已达到门槛，可转人工催件。" if stale else "尚未达到门槛，请继续关注轨迹。"))
    return missing("该规则暂不支持自动判断。")
