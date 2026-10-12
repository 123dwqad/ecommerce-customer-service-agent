import tempfile
import unittest
from pathlib import Path
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from app.commerce.config import Settings
from app.commerce.store import Store, Order, AfterSale
from app.commerce.db_models import Product, SKU, OrderItem, Shipment, TrackingEvent, AfterSaleDetail, AfterSaleEvent
from app.commerce.seed import seed_scenarios, table_counts
from app.commerce.service import CommerceService, BusinessError
from app.commerce.knowledge import Knowledge


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(database_url="sqlite:///" + str(Path(self.temp.name) / "seed.db"))
        self.store = Store(self.settings.database_url)
        self.store.initialize()
        self.accounts = [{"user_id": f"demo_buyer_{n:02}", "password": "local-test-password"} for n in range(1, 3)]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_idempotent_preserves_existing_and_consistent_money_and_history(self):
        self.store.register("existing", "local-test-password")
        self.store.seed_orders("existing")
        with self.store.session() as db:
            before = {o.id: (o.status, o.version, o.details.copy()) for o in db.scalars(select(Order))}
        self.assertTrue(seed_scenarios(self.store, self.accounts))
        counts = table_counts(self.store)
        self.assertEqual(counts["commerce_products"], 18)
        self.assertEqual(counts["commerce_skus"], 54)
        self.assertEqual(counts["commerce_orders"], 27)
        self.assertEqual(counts["commerce_order_items"], 27)
        self.assertFalse(seed_scenarios(self.store, self.accounts))
        self.store.initialize()
        self.assertEqual(counts, table_counts(self.store))
        with self.store.session() as db:
            for oid, facts in before.items():
                o = db.get(Order, oid)
                self.assertEqual(facts, (o.status, o.version, o.details))
            for item in db.scalars(select(OrderItem)):
                order = db.get(Order, item.order_id)
                self.assertEqual(item.unit_price_cents * item.quantity, order.details["merchandise_cents"])
                self.assertEqual(item.paid_cents, order.details["paid_merchandise_cents"])
            for shipment in db.scalars(select(Shipment)):
                events = list(db.scalars(select(TrackingEvent).where(TrackingEvent.shipment_id == shipment.id).order_by(TrackingEvent.occurred_at)))
                self.assertEqual(len(events), 3)
                self.assertGreaterEqual(events[0].occurred_at, shipment.shipped_at)
                if shipment.signed_at:
                    self.assertEqual(events[-1].occurred_at, shipment.signed_at)
            for sale in db.scalars(select(AfterSale)):
                detail = db.get(AfterSaleDetail, sale.id)
                self.assertIsNotNone(detail)
                last = db.scalar(select(AfterSaleEvent).where(AfterSaleEvent.after_sale_id == sale.id).order_by(AfterSaleEvent.occurred_at.desc()))
                self.assertEqual(last.status, sale.status)

    def test_database_is_read_source_and_new_operations_create_details(self):
        seed_scenarios(self.store, self.accounts)
        service = CommerceService(self.store, Knowledge(self.settings))
        actor = self.store.actor(self.store.login("demo_buyer_01", "local-test-password"))
        with self.store.session.begin() as db:
            db.get(Product, "P100").description = "数据库中的新商品说明"
            for sku in db.scalars(select(SKU).where(SKU.product_id == "P100")):
                sku.stock = 0
        result = service.invoke("search_knowledge", {"query": "P100", "product_id": "P100"}, *actor)
        self.assertIn("数据库中的新商品说明", result["hits"][0]["text"])
        result = service.invoke("check_business_rule", {"topic": "purchase", "product_id": "P100", "quantity": 1}, *actor)
        self.assertEqual(result["status"], "not_eligible")
        result = service.invoke("query_logistics", {"order_id": "ODEMO001005"}, *actor)
        self.assertEqual(len(result["events"]), 3)
        self.assertEqual(len(result["shipments"]), 1)
        with self.assertRaises(BusinessError):
            service.invoke("query_logistics", {"order_id": "ODEMO002005"}, *actor)
        prepared = service.invoke("prepare_operation", {"order_id": "ODEMO001007", "action": "create_after_sale"}, *actor)
        result = service.invoke("confirm_operation", {"confirmation_id": prepared["confirmation_id"]}, *actor)
        with self.store.session() as db:
            self.assertIsNotNone(db.get(AfterSaleDetail, result["after_sale_id"]))

    def test_invalid_child_record_rejected(self):
        with self.assertRaises(IntegrityError):
            with self.store.session.begin() as db:
                db.add(SKU(id="orphan", product_id="missing", specifications={}, price_cents=100, stock=1))

    def test_account_collision_rolls_back_entire_batch(self):
        self.store.register("demo_buyer_02", "existing-password")
        counts = table_counts(self.store)
        with self.assertRaises(ValueError):
            seed_scenarios(self.store, self.accounts)
        self.assertEqual(counts, table_counts(self.store))
