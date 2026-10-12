"""Add a synthetic multi-stop shipment trace to one existing demo order."""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.commerce.config import Settings
from app.commerce.db_models import Shipment, TrackingEvent
from app.commerce.seed import demo_tracking_data, enrich_order
from app.commerce.store import Order, Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("order_id", help="The shipped demo order to populate")
    args = parser.parse_args()

    store = Store(Settings().database_url)
    try:
        with store.session.begin() as db:
            order = db.get(Order, args.order_id)
            if order is None:
                parser.error(f"Order not found: {args.order_id}")
            if order.status != "shipped":
                parser.error(f"Order must be in shipped state: {args.order_id}")
            if db.scalar(select(Shipment.id).where(Shipment.order_id == order.id)):
                print(f"Shipment already exists for {order.id}; no changes made.")
                return 0

            details = dict(order.details or {})
            details.update(demo_tracking_data(order.status))
            order.details = details
            db.flush()
            enrich_order(db, order)
            db.flush()

            shipment = db.scalar(select(Shipment).where(Shipment.order_id == order.id))
            if shipment is None:
                raise RuntimeError(f"Could not create shipment for {order.id}")
            # Query after the insert without exposing recipient details.
            count = len(list(db.scalars(select(TrackingEvent).where(
                TrackingEvent.shipment_id == shipment.id))))
            print(f"Added 1 simulated shipment and {count} tracking events for {order.id}.")
        return 0
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
