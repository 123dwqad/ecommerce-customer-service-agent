# -*- coding: utf-8 -*-
"""RAG 服务门面：入库 + 检索，输出与 search_knowledge 兼容的工具结果。

- RAG 状态存放在独立库（默认 data/rag.sqlite），不随业务库（COMMERCE_DATABASE_URL）变化，
  预构建一次即可被 API / MCP / 测试进程共享；
- 索引未就绪时触发后台构建，当前请求抛 RagNotReady，由调用方回退 search_knowledge。

hit 结构兼容 agent.py reply() 与 react_answer()：
{"id", "title", "text", "source", "version", "locator", "score", "backend"}
"""
from __future__ import annotations

import logging
import threading

from app.commerce.rag.retrieval import RetrievalService

logger = logging.getLogger(__name__)


class RagNotReady(RuntimeError):
    """索引尚未构建完成，调用方应回退关键词检索。"""


class RAGService:
    def __init__(self, store, settings):
        from pathlib import Path

        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker

        from app.commerce.db_models import RagChunk, RagDocument
        from app.commerce.rag.embeddings import get_embedding_client
        from app.commerce.rag.ingest import IngestService
        from app.commerce.rag.rewrite import RewriteService
        from app.commerce.rag.stores import KeywordStore, QdrantStore
        self.settings = settings
        rag_path = Path(settings.rag_database_path)
        rag_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{rag_path}",
                                    connect_args={"check_same_thread": False, "timeout": 15})
        event.listen(self.engine, "connect",
                     lambda conn, _: (conn.execute("PRAGMA journal_mode=WAL"), conn.execute("PRAGMA foreign_keys=ON")))
        RagDocument.__table__.create(self.engine, checkfirst=True)
        RagChunk.__table__.create(self.engine, checkfirst=True)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._build_lock = threading.Lock()
        self._build_started = False

        self.rewriter = RewriteService(
            api_key=settings.api_key,
            base_url=settings.rag_llm_base_url,
            model=settings.rag_rewrite_model,
            timeout=settings.rag_llm_timeout_seconds,
        )
        self.embedder = get_embedding_client(
            settings.rag_embedding_provider,
            settings.api_key,
            settings.rag_embedding_base_url,
            settings.rag_embedding_model,
        )
        self.qdrant = QdrantStore(
            path=settings.rag_qdrant_path,
            collection_name=settings.rag_collection_name,
        )
        self.keyword = KeywordStore(
            es_url=settings.rag_es_url,
            es_index=settings.rag_es_index,
            timeout=settings.rag_es_timeout_seconds,
            circuit_seconds=settings.rag_es_circuit_seconds,
        )
        store_adapter = _StoreAdapter(self.session, self.engine)
        self.retrieval = RetrievalService(
            store=store_adapter, qdrant_store=self.qdrant, keyword_store=self.keyword,
            embedder=self.embedder, rewriter=self.rewriter, settings=settings,
        )
        self.ingest = IngestService(
            store=store_adapter, qdrant_store=self.qdrant, keyword_store=self.keyword,
            embedder=self.embedder, settings=settings, rewriter=self.rewriter,
        )

    # ---------------- 就绪检查与构建 ----------------
    def ready(self) -> bool:
        if not self.qdrant.exists():
            return False
        from sqlalchemy import func, select
        from app.commerce.db_models import RagDocument
        with self.session() as db:
            return db.scalar(select(func.count(RagDocument.document_key))) > 0

    def ensure_index(self):
        """同步构建（构建脚本 / 首次联调用）；索引已就绪时不做任何事。"""
        if self.ready():
            return None
        with self._build_lock:
            if self.ready():
                return None
            return self.ingest.build_all(force=False)

    def _start_background_build(self):
        if self._build_started:
            return
        self._build_started = True
        threading.Thread(target=self.ensure_index, name="rag-index-build", daemon=True).start()

    def build(self, *, force: bool = False):
        with self._build_lock:
            return self.ingest.build_all(force=force)

    # ---------------- 检索 ----------------
    def search(self, *, query: str, product_id: str | None = None,
               history: list | None = None, top_k: int | None = None) -> dict:
        if not self.ready():
            # 索引未就绪：后台构建，本次请求让调用方回退关键词检索，避免长请求。
            self._start_background_build()
            raise RagNotReady("rag index is building")
        plan, chunks = self.retrieval.retrieve(
            question=query,
            history=[item for item in (history or []) if isinstance(item, dict)
                     and item.get("role") in {"user", "assistant"} and item.get("content")],
            top_k=top_k,
            product_id=product_id,
        )
        hits = []
        for chunk in chunks:
            hits.append({
                "id": chunk.chunk_key,
                "title": chunk.title,
                "text": chunk.content[:600],
                "source": f"rag://demo/{chunk.source_name}#{chunk.locator or ''}",
                "version": "rag-demo-v1",
                "locator": chunk.locator,
                "page_no": chunk.page_no,
                "chunk_type": chunk.chunk_type,
                "score": round(float(chunk.score), 4),
                "backend": chunk.backend,
            })
        return {
            "status": "ok" if hits else "no_results",
            "hits": hits,
            "plan": {
                "route": plan.route,
                "rewritten_query": plan.rewritten_query,
                "step_back_query": plan.step_back_query,
                "es_candidate_count": plan.es_candidate_count,
                "qdrant_candidate_count": plan.qdrant_candidate_count,
                "fused_count": plan.fused_count,
                "parent_context_count": plan.parent_context_count,
                "fallback_backend": plan.fallback_backend,
            },
        }


class _StoreAdapter:
    """为 ingest/retrieval 提供与 Store 一致的 session / session.begin() 接口，底层用 RAG 独立库。"""

    def __init__(self, session_factory, engine):
        self.session = session_factory  # sessionmaker：session() 与 session.begin() 均可用
        self.engine = engine
