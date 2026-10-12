# -*- coding: utf-8 -*-
"""电商知识库检索编排：

历史压缩 → Query 改写 → Step Back → 意图路由（keyword/vector/hybrid）
→ 多路召回（关键词 BM25/ES + 向量 Qdrant，原问题与回溯问题并行）
→ RRF 融合 → Top-K → 父块上下文补充。
重排器保留可选扩展位，默认不启用。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select

from app.commerce.rag.schemas import RetrievedChunk, RetrievalPlan

logger = logging.getLogger(__name__)


class RetrievalService:
    def __init__(self, *, store, qdrant_store, keyword_store, embedder, rewriter, settings):
        self.store = store
        self.qdrant = qdrant_store
        self.keyword = keyword_store
        self.embedder = embedder
        self.rewriter = rewriter
        self.settings = settings
        self._bm25 = None

    def retrieve(
        self,
        *,
        question: str,
        history: list[dict] | None = None,
        top_k: int | None = None,
        product_id: str | None = None,
    ) -> tuple[RetrievalPlan, list[RetrievedChunk]]:
        history = history or []
        rewritten = self._rewrite_question(question, history)
        step_back = self._generate_step_back(rewritten)
        route = self._detect_route(rewritten)
        plan = RetrievalPlan(
            route=route,
            rewritten_query=rewritten,
            step_back_query=step_back if step_back != rewritten else None,
            qdrant_available=self.qdrant is not None,
            es_available=self.keyword.available(),
        )

        candidate_top_k = max(self.settings.rag_candidate_top_k, top_k or self.settings.rag_top_k)
        results_by_backend = self._run_retrieval_branches(
            route=route,
            rewritten_query=rewritten,
            step_back_query=step_back,
            top_k=candidate_top_k,
            product_id=product_id,
            plan=plan,
        )
        if not results_by_backend:
            fallback = self._search_sqlite_fallback(rewritten, candidate_top_k, product_id)
            plan.fallback_backend = "sqlite"
            plan.fused_count = len(fallback)
            return plan, self._attach_parent_context(fallback, plan)

        fused = self._reciprocal_rank_fusion(results_by_backend)
        plan.fused_count = len(fused)
        final_top_k = top_k or self.settings.rag_top_k
        return plan, self._attach_parent_context(fused[:final_top_k], plan)

    # ---------------- Query 处理链 ----------------
    def _rewrite_question(self, question: str, history: list[dict]) -> str:
        if not self.settings.rag_rewrite_enabled or not history:
            return question
        try:
            prepared = self.rewriter.summarize_history(
                history,
                threshold_turns=6,
                keep_recent=3,
                max_recent_chars=2000,
                max_source_chars=4000,
                max_summary_tokens=160,
            )
            return self.rewriter.rewrite_question(question=question, history=prepared)
        except Exception:
            return question

    def _generate_step_back(self, query: str) -> str:
        if not self.settings.rag_rewrite_enabled:
            return query
        try:
            return self.rewriter.generate_step_back_query(question=query)
        except Exception:
            return query

    def _detect_route(self, query: str) -> str:
        lowered = query.lower()
        exact_patterns = ["第几条", "第几章", "表格", "目录", "价格", "多少钱", "规格", "参数", "编号",
                          "几件", "多少", "材料", "步骤", "表", "页", "code", "page"]
        semantic_patterns = ["怎么", "如何", "流程", "注意事项", "适用", "区别", "原因", "怎么办", "能", "可以", "can i", "how", "why"]
        exact_score = sum(1 for pattern in exact_patterns if pattern in lowered)
        semantic_score = sum(1 for pattern in semantic_patterns if pattern in lowered)
        if exact_score >= 2 and semantic_score == 0:
            return "keyword"
        if semantic_score >= 2 and exact_score == 0:
            return "vector"
        return "hybrid"

    # ---------------- 多路召回 ----------------
    def _run_retrieval_branches(self, *, route, rewritten_query, step_back_query, top_k, product_id, plan) -> list:
        tasks: list[tuple[str, str]] = []
        if route in {"keyword", "hybrid"}:
            tasks.append(("primary", rewritten_query))
        if route in {"vector", "hybrid"}:
            tasks.append(("primary", rewritten_query))
        if step_back_query and step_back_query != rewritten_query:
            if route in {"keyword", "hybrid"}:
                tasks.append(("step_back", step_back_query))
            if route in {"vector", "hybrid"}:
                tasks.append(("step_back", step_back_query))
        if not tasks:
            return []
        results_by_backend: list = []
        branch_count = len(tasks) * (2 if route == "hybrid" else 1)
        with ThreadPoolExecutor(max_workers=max(1, min(branch_count, self.settings.rag_parallel_workers))) as executor:
            futures = {}
            for label, query in tasks:
                if route in {"keyword", "hybrid"}:
                    futures[executor.submit(self._run_keyword_branch, query, top_k, product_id)] = ("keyword", label, query)
                if route in {"vector", "hybrid"}:
                    futures[executor.submit(self._run_vector_branch, query, top_k, product_id)] = ("vector", label, query)
            for future in as_completed(futures):
                backend, label, query = futures[future]
                try:
                    results = future.result()
                except Exception as exc:
                    logger.warning("%s/%s retrieval branch failed: %s", backend, label, exc)
                    results = []
                if label == "primary" and backend == "keyword":
                    plan.es_candidate_count = len(results)
                if label == "primary" and backend == "vector":
                    plan.qdrant_candidate_count = len(results)
                if results:
                    results_by_backend.append(results)
        return results_by_backend

    def _run_keyword_branch(self, query: str, top_k: int, product_id: str | None) -> list[RetrievedChunk]:
        if self.keyword.available():
            return self.keyword.search(query, top_k=top_k, product_id=product_id)
        bm25 = self._get_bm25()
        if bm25:
            return bm25.search(query, top_k=top_k, product_id=product_id)
        return self._search_sqlite_fallback(query, top_k, product_id)

    def _run_vector_branch(self, query: str, top_k: int, product_id: str | None) -> list[RetrievedChunk]:
        if self.qdrant is None:
            return []
        try:
            vector = self.embedder.embed_query(query)
            return self.qdrant.search(vector, top_k=top_k, product_id=product_id)
        except Exception as exc:
            logger.warning("Qdrant search failed, skipping vector branch: %s", exc)
            return []

    def _get_bm25(self):
        if self._bm25 is not None:
            return self._bm25
        from app.commerce.rag.stores import BM25Lite
        from app.commerce.rag.schemas import ChunkPayload
        try:
            from app.commerce.db_models import RagChunk, RagDocument
            with self.store.session() as db:
                rows = db.execute(select(RagChunk, RagDocument.keywords).join(
                    RagDocument, RagDocument.document_key == RagChunk.document_key)).all()
            chunks = []
            for row in rows:
                chunk, keywords = row
                # Parent summaries provide context after a child is retrieved;
                # including both levels in BM25 creates duplicate hits.
                if chunk.representation_level != "fine":
                    continue
                meta = dict(chunk.chunk_metadata or {})
                meta.setdefault("document_key", chunk.document_key)
                meta.setdefault("keywords", list(keywords or []))
                chunks.append(ChunkPayload(
                    chunk_key=chunk.chunk_key, parent_chunk_key=chunk.parent_chunk_key,
                    representation_level=chunk.representation_level, chunk_type=chunk.chunk_type,
                    content=chunk.content, title=chunk.title, source_name=chunk.source_name,
                    page_no=chunk.page_no, locator=chunk.locator, chunk_metadata=meta,
                ))
            self._bm25 = BM25Lite(chunks) if chunks else None
        except Exception as exc:
            logger.warning("BM25 index unavailable: %s", exc)
            self._bm25 = None
        return self._bm25

    # ---------------- SQLite 兜底 ----------------
    def _search_sqlite_fallback(self, query: str, top_k: int, product_id: str | None) -> list[RetrievedChunk]:
        from app.commerce.db_models import RagChunk, RagDocument
        results: list = []
        try:
            with self.store.session() as db:
                terms = [term for term in re_split(query) if len(term) >= 2][:5]
                for term in terms or [query[:8]]:
                    stmt = (select(RagChunk, RagDocument.keywords).join(
                        RagDocument, RagDocument.document_key == RagChunk.document_key)
                        .where(RagChunk.representation_level == "fine")
                        .where(RagChunk.content.like(f"%{term}%"))
                        .limit(top_k))
                    rows = db.execute(stmt).all()
                    for row in rows:
                        chunk, keywords = row
                        if product_id and product_id not in (keywords or []):
                            continue
                        results.append(_sqlite_row_to_chunk(chunk, backend="sqlite"))
                    if len(results) >= top_k:
                        break
                seen = set()
                unique = []
                for chunk in results:
                    if chunk.chunk_key not in seen:
                        seen.add(chunk.chunk_key)
                        unique.append(chunk)
                return unique[:top_k]
        except Exception as exc:
            logger.warning("SQLite fallback search failed: %s", exc)
            return []

    # ---------------- 融合与父块补充 ----------------
    def _reciprocal_rank_fusion(self, result_sets: list, k: int = 60) -> list[RetrievedChunk]:
        scores: dict[str, float] = defaultdict(float)
        by_key: dict[str, RetrievedChunk] = {}
        for result_set in result_sets:
            for rank, chunk in enumerate(result_set):
                scores[chunk.chunk_key] += 1.0 / (k + rank + 1)
                current = by_key.get(chunk.chunk_key)
                if current is None or chunk.score > current.score:
                    by_key[chunk.chunk_key] = chunk
        sorted_keys = sorted(scores, key=lambda key: scores[key], reverse=True)
        fused: list[RetrievedChunk] = []
        for key in sorted_keys:
            item = by_key[key]
            item.score = scores[key]
            fused.append(item)
        return fused

    def _attach_parent_context(self, chunks: list[RetrievedChunk], plan: RetrievalPlan) -> list[RetrievedChunk]:
        """为命中的子块补充持久化的父块摘要，提供章节背景。"""
        if not chunks or not self.settings.rag_parent_enabled:
            return chunks
        from app.commerce.db_models import RagChunk
        child_keys = [chunk.chunk_key for chunk in chunks]
        parent_key_by_child: dict[str, str] = {}
        try:
            with self.store.session() as db:
                rows = db.execute(select(RagChunk.chunk_key, RagChunk.parent_chunk_key)
                                  .where(RagChunk.chunk_key.in_(child_keys))).all()
                parent_key_by_child = {row[0]: row[1] for row in rows if row[1]}
                parent_keys = list({key for key in parent_key_by_child.values()})
                if parent_keys:
                    parent_rows = db.execute(select(RagChunk.chunk_key, RagChunk.content, RagChunk.locator)
                                             .where(RagChunk.chunk_key.in_(parent_keys))
                                             .where(RagChunk.representation_level == "coarse")).all()
                    parents = {row[0]: row for row in parent_rows}
                    attached = 0
                    for chunk in chunks:
                        parent_key = parent_key_by_child.get(chunk.chunk_key)
                        if not parent_key:
                            continue
                        chunk.parent_chunk_key = parent_key
                        parent = parents.get(parent_key)
                        if parent is None:
                            continue
                        chunk.parent_content = parent[1]
                        chunk.parent_locator = parent[2]
                        attached += 1
                    plan.parent_context_count = attached
        except Exception as exc:
            logger.warning("Attach parent context failed: %s", exc)
        return chunks


def _sqlite_row_to_chunk(chunk, *, backend: str) -> RetrievedChunk:
    meta = dict(chunk.chunk_metadata or {})
    return RetrievedChunk(
        chunk_key=chunk.chunk_key,
        parent_chunk_key=chunk.parent_chunk_key,
        document_key=meta.get("document_key", chunk.document_key),
        title=chunk.title,
        source_name=chunk.source_name,
        source_type="pdf",
        permission_level="public",
        representation_level=chunk.representation_level,
        chunk_type=chunk.chunk_type,
        content=chunk.content,
        page_no=chunk.page_no,
        locator=chunk.locator,
        chunk_metadata=meta,
        score=0.0,
        backend=backend,
    )


def re_split(text: str) -> list:
    import re
    normalized = " ".join(text.lower().split())
    return re.findall(r"[a-z0-9_]+|[一-鿿]{2,}", normalized)
