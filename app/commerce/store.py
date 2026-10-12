from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import JSON, Float, Integer, String, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "commerce_users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    salt: Mapped[str] = mapped_column(String(64))
    password: Mapped[str] = mapped_column(String(128))
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)


class Login(Base):
    __tablename__ = "commerce_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    expires: Mapped[float] = mapped_column(Float)


class Order(Base):
    __tablename__ = "commerce_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    product_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    signed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    details: Mapped[dict] = mapped_column(JSON)


class Confirmation(Base):
    __tablename__ = "commerce_confirmations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_hash: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(String(64))
    order_version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    expires: Mapped[float] = mapped_column(Float)
    consumed: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AfterSale(Base):
    __tablename__ = "commerce_after_sales"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="submitted")
    policy_version: Mapped[str] = mapped_column(String(64))


class Handoff(Base):
    __tablename__ = "commerce_handoffs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64))
    created: Mapped[float] = mapped_column(Float, default=time.time)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260000).hex()


class Store:
    def __init__(self, url: str):
        if url.startswith("sqlite:///:"):
            raise ValueError("Use a temporary file instead of in-memory SQLite for concurrent requests")
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, pool_pre_ping=True, **(
            {"connect_args": {"check_same_thread": False, "timeout": 15}} if url.startswith("sqlite") else {}
        ))
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def pragmas(conn, _):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        from app.commerce.seed import seed_catalog
        Base.metadata.create_all(self.engine)
        with self.session.begin() as db:
            seed_catalog(db)

    def register(self, user_id: str, password: str):
        salt = secrets.token_hex(16)
        with self.session.begin() as db:
            db.add(User(id=user_id, salt=salt, password=password_hash(password, salt)))

    def login(self, user_id: str, password: str) -> str:
        with self.session.begin() as db:
            user = db.get(User, user_id)
            salt = user.salt if user else "00" * 16
            candidate = password_hash(password, salt)
            if not user or not hmac.compare_digest(candidate, user.password):
                raise PermissionError("账号或密码错误")
            token = secrets.token_urlsafe(32)
            db.add(Login(token_hash=digest(token), user_id=user.id, expires=time.time() + 86400))
            return token

    def actor(self, token: str) -> tuple[str, str]:
        return self.actor_from_session(digest(token))

    def actor_from_session(self, session_hash: str) -> tuple[str, str]:
        with self.session() as db:
            login = db.get(Login, session_hash)
            if not login or login.expires <= time.time():
                raise PermissionError("请先登录，或重新登录后再试")
            return login.user_id, login.token_hash

    def logout(self, token: str):
        with self.session.begin() as db:
            login = db.get(Login, digest(token))
            if login:
                db.delete(login)

    def seed_orders(self, user_id: str):
        """Explicit demo action, called after registration; never seeds other users' accounts."""
        from app.commerce.seed import demo_tracking_data
        from app.commerce.knowledge import PRODUCTS
        from app.commerce.rules import RULES
        now = time.time()
        with self.session.begin() as db:
            if db.scalar(select(Order).where(Order.user_id == user_id)):
                return
            for suffix, status, product in [("01", "paid", "P100"), ("02", "shipped", "P100"), ("03", "delivered", "P200")]:
                purchased_at = now - 5 * 86400
                signed_at = now - 2 * 86400 if status == "delivered" else None
                tracking = demo_tracking_data(status, now=now, signed_at=signed_at)
                tracking_data = tracking or {"tracking": [{"status": status,
                    "description": {"paid": "待发货"}.get(status, status)}]}
                details = {"merchant_id": "demo", "channel": "web", "purchased_at": purchased_at,
                             "rule_versions": {r["topic"]: r["version"] for r in RULES
                                 if PRODUCTS[product]["category"] in r["categories"]
                                 and datetime.fromisoformat(r["effective_from"]).timestamp() <= purchased_at
                                 and (not r.get("effective_to") or purchased_at < datetime.fromisoformat(r["effective_to"]).timestamp())},
                             "quantity": 1, "merchandise_cents": PRODUCTS[product]["price_cents"],
                             "paid_merchandise_cents": PRODUCTS[product]["price_cents"], "discount_cents": 0,
                             "paid_at": now - (3600 if status == "paid" else 5 * 86400),
                             "shipping_region": "standard", "invoice_status": "not_requested",
                             "production_started": False, "last_logistics_at": tracking_data.get("last_logistics_at"),
                             "phone": "13800138000", "address": "演示市演示路 1 号", "recipient": "演示收件人",
                             "tracking": tracking_data["tracking"]}
                if tracking:
                    details["shipped_at"] = tracking["shipped_at"]
                db.add(Order(
                    id=f"O{secrets.token_hex(4).upper()}{suffix}", user_id=user_id,
                    product_id=product, status=status,
                    signed_at=signed_at,
                    policy_version="RETURN-DEMO-v1", version=1,
                    details=details,
                ))
            db.flush()
            from app.commerce.seed import enrich_order
            for order in db.scalars(select(Order).where(Order.user_id == user_id)):
                enrich_order(db, order)
