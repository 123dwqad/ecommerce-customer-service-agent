"""Additive schema: existing order columns remain compatible with the original demo."""
import time
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, JSON, String, Float, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.commerce.store import Base


class Product(Base):
    __tablename__ = "commerce_products"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32), index=True)
    brand: Mapped[str] = mapped_column(String(64), default="演示自营")
    description: Mapped[str] = mapped_column(String(2000))
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    customized: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="active")


class SKU(Base):
    __tablename__ = "commerce_skus"
    __table_args__ = (CheckConstraint("price_cents >= 0 AND stock >= 0"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("commerce_products.id"), index=True)
    specifications: Mapped[dict] = mapped_column(JSON)
    price_cents: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="active")


class PolicyRecord(Base):
    __tablename__ = "commerce_policy_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(32), index=True)
    merchant_id: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSON)


class OrderItem(Base):
    __tablename__ = "commerce_order_items"
    __table_args__ = (CheckConstraint("quantity > 0 AND unit_price_cents >= 0 AND paid_cents >= 0"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("commerce_orders.id"), index=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("commerce_skus.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    paid_cents: Mapped[int] = mapped_column(Integer)
    product_snapshot: Mapped[dict] = mapped_column(JSON)
    policy_version: Mapped[str] = mapped_column(String(64))


class Shipment(Base):
    __tablename__ = "commerce_shipments"
    __table_args__ = (UniqueConstraint("carrier", "tracking_number"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("commerce_orders.id"), index=True)
    carrier: Mapped[str] = mapped_column(String(64))
    tracking_number: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32))
    shipped_at: Mapped[float] = mapped_column(Float)
    signed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class TrackingEvent(Base):
    __tablename__ = "commerce_tracking_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("commerce_shipments.id"), index=True)
    occurred_at: Mapped[float] = mapped_column(Float, index=True)
    status: Mapped[str] = mapped_column(String(32))
    location: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(500))


class AfterSaleDetail(Base):
    __tablename__ = "commerce_after_sale_details"
    __table_args__ = (CheckConstraint("quantity > 0 AND requested_cents >= 0"),)
    after_sale_id: Mapped[str] = mapped_column(ForeignKey("commerce_after_sales.id"), primary_key=True)
    order_item_id: Mapped[str] = mapped_column(ForeignKey("commerce_order_items.id"), index=True)
    request_type: Mapped[str] = mapped_column(String(32), default="return_refund")
    quantity: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500))
    requested_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class AfterSaleEvent(Base):
    __tablename__ = "commerce_after_sale_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    after_sale_id: Mapped[str] = mapped_column(ForeignKey("commerce_after_sales.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(String(500))


class OrderEvent(Base):
    __tablename__ = "commerce_order_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("commerce_orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(String(500))


class SeedBatch(Base):
    __tablename__ = "commerce_seed_batches"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    summary: Mapped[dict] = mapped_column(JSON)


class RagDocument(Base):
    __tablename__ = "commerce_rag_documents"
    document_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    source_name: Mapped[str] = mapped_column(String(200))
    source_path: Mapped[str] = mapped_column(String(500))
    file_mtime: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="indexed")
    child_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    parent_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class RagChunk(Base):
    __tablename__ = "commerce_rag_chunks"
    chunk_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_key: Mapped[str] = mapped_column(ForeignKey("commerce_rag_documents.document_key"), index=True)
    parent_chunk_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    representation_level: Mapped[str] = mapped_column(String(16), index=True)  # fine / coarse
    chunk_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    source_name: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(String(4000))
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locator: Mapped[str | None] = mapped_column(String(200), nullable=True)
    chunk_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
