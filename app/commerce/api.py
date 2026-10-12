from __future__ import annotations

import logging
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Path as ApiPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError

from app.commerce.agent import CommerceAgent
from app.commerce.config import Settings
from app.commerce.knowledge import Knowledge
from app.commerce.memory import ConversationMemory
from app.commerce.service import BusinessError, CommerceService
from app.commerce.store import Store, User
from app.commerce.tools import ToolExecutor
from app.commerce.multimodal import (MultimodalService, MediaError, VisionInput,
    read_bounded, MAX_AUDIO_BYTES, MAX_IMAGE_REQUEST_BYTES)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Body):
    user_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,64}$")
    password: str = Field(min_length=8, max_length=128)


class ChatBody(Body):
    query: str = Field(min_length=1, max_length=1500)
    conversation_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ConfirmBody(Body):
    confirmation_id: str = Field(min_length=20, max_length=64)


class PreferenceBody(Body):
    favorite_categories: list[Literal["electronics", "clothing", "custom"]] = Field(default_factory=list, max_length=3)
    answer_style: Literal["concise", "detailed"] = "concise"


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    settings.validate()
    store = Store(settings.database_url)
    service = CommerceService(store, Knowledge(settings))
    memory = ConversationMemory(settings.redis_url)
    agent = CommerceAgent(settings, ToolExecutor(settings, service), memory)
    media = MultimodalService(settings)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield
        await agent.aclose()
        await memory.close()
        store.engine.dispose()

    app = FastAPI(title="电商智能客服 Agent", version="1.0.0", lifespan=lifespan)
    app.state.store, app.state.service, app.state.agent = store, service, agent
    app.state.media = media

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        provided = request.headers.get("X-Request-ID", "")
        request_id = provided if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", provided) else uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        response.headers.update({"X-Request-ID": request_id, "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
        logging.getLogger(__name__).info("request_id=%s method=%s status=%s latency_ms=%.2f",
            request_id, request.method, response.status_code, (time.perf_counter() - start) * 1000)
        return response

    def error(request, code, message, status):
        return JSONResponse(status_code=status, content={"code": code, "message": message,
            "request_id": getattr(request.state, "request_id", uuid4().hex)})

    @app.exception_handler(BusinessError)
    async def business_error(request, exc):
        return error(request, exc.code, str(exc), 409)

    @app.exception_handler(MediaError)
    async def media_error(request, exc):
        return error(request, exc.code, str(exc), exc.status)

    @app.exception_handler(PermissionError)
    async def auth_error(request, exc):
        return error(request, "unauthorized", str(exc), 401)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(request, "invalid_request", "请求字段不合法，请检查输入。", 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger(__name__).error("request_id=%s error_type=%s", getattr(request.state, "request_id", ""), type(exc).__name__)
        return error(request, "service_unavailable", "服务暂时不可用，请稍后重试。", 503)

    def actor(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise PermissionError("请先登录")
        return store.actor(authorization[7:])

    @app.get("/health")
    def health():
        return {"status": "ok", "domain": "commerce", "business_backend": "demo",
                "tool_transport": settings.tool_transport, "llm_enabled": settings.llm_enabled,
                "memory_backend": "redis" if settings.redis_url else "local_single_process",
                "demo_enabled": settings.demo_enabled}

    @app.post("/auth/register", status_code=201)
    def register(body: Credentials):
        try:
            store.register(body.user_id, body.password)
        except IntegrityError:
            raise HTTPException(409, "账号已存在")
        if settings.demo_enabled:
            store.seed_orders(body.user_id)
        return {"user_id": body.user_id, "message": "注册成功", "demo_orders_created": settings.demo_enabled}

    @app.post("/auth/login")
    def login(body: Credentials):
        return {"access_token": store.login(body.user_id, body.password), "token_type": "bearer", "user_id": body.user_id}

    @app.get("/auth/me")
    def me(current=Depends(actor)):
        return {"user_id": current[0]}

    @app.post("/auth/logout")
    def logout(current=Depends(actor), authorization: str = Header()):
        store.logout(authorization[7:])
        return {"status": "ok"}

    @app.get("/orders")
    def orders(current=Depends(actor)):
        return service.invoke("list_orders", {}, *current)

    @app.get("/orders/{order_id}")
    def order(order_id: str, current=Depends(actor)):
        return service.invoke("query_order", {"order_id": order_id}, *current)

    @app.get("/orders/{order_id}/logistics")
    def logistics(order_id: str, current=Depends(actor)):
        return service.invoke("query_logistics", {"order_id": order_id}, *current)

    @app.get("/orders/{order_id}/after-sales")
    def order_after_sales(order_id: str = ApiPath(pattern=r"^O[A-Z0-9]{3,40}$"), current=Depends(actor)):
        return service.invoke("query_after_sale", {"order_id": order_id}, *current)

    @app.get("/after-sales/{after_sale_id}")
    def after_sale(after_sale_id: str = ApiPath(pattern=r"^[Aa][Ss][A-Za-z0-9]{3,40}$"), current=Depends(actor)):
        return service.invoke("query_after_sale", {"after_sale_id": after_sale_id}, *current)

    @app.get("/preferences")
    def preferences(current=Depends(actor)):
        with store.session() as db:
            return db.get(User, current[0]).preferences

    @app.put("/preferences")
    def save_preferences(body: PreferenceBody, current=Depends(actor)):
        with store.session.begin() as db:
            db.get(User, current[0]).preferences = body.model_dump()
        return body.model_dump()

    @app.post("/chat/")
    async def chat(body: ChatBody, request: Request, current=Depends(actor)):
        if not body.query.strip():
            raise HTTPException(422, "消息不能为空")
        return await agent.chat(current, body.query, body.conversation_id, request.state.request_id)

    @app.post("/operations/confirm")
    def confirm(body: ConfirmBody, current=Depends(actor)):
        return service.invoke("confirm_operation", {"confirmation_id": body.confirmation_id}, *current)

    @app.get("/media/options")
    def media_options():
        return media.options()

    @app.post("/media/transcribe")
    async def transcribe(request: Request, current=Depends(actor)):
        media.configured("speech")
        if request.headers.get("content-type", "").split(";")[0] not in {"audio/wav", "audio/x-wav", "audio/wave"}:
            raise MediaError("unsupported_audio", "请使用 WAV 音频上传。", 415)
        raw = await read_bounded(request, MAX_AUDIO_BYTES)
        return await media.transcribe(raw, request.state.request_id)

    @app.post("/media/vision")
    async def vision(request: Request, current=Depends(actor)):
        media.configured("vision")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise MediaError("invalid_upload", "图片请求需使用 JSON 格式。", 415)
        raw = await read_bounded(request, MAX_IMAGE_REQUEST_BYTES)
        try:
            body = VisionInput.model_validate(json.loads(raw))
        except (ValueError, UnicodeDecodeError, ValidationError):
            raise MediaError("invalid_upload", "图片或问题字段不合法，请重新选择图片。", 422)
        return await media.analyze(body, request.state.request_id)

    frontend = Path(__file__).resolve().parents[2] / "frontend"
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
