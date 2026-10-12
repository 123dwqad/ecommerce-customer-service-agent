# -*- coding: utf-8 -*-
"""检索后端存储：本地 Qdrant、可选 Elasticsearch，以及 BM25/SQLite LIKE 关键词兜底。"""
from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path
from threading import Lock

from app.commerce.rag.schemas import ChunkPayload, RetrievedChunk

logger = logging.getLogger(__name__)

_es_circuit_lock = Lock()
_es_circuit_opened_at = 0.0


class QdrantStore:
    """本地嵌入式 Qdrant；QDRANT_URL 未配置时落盘到本地目录。"""

    def __init__(self, *, path: str, collection_name: str, dim: int = 384):
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm
        self._models = qm
        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = QdrantClient(path=path)
        self.collection_name = collection_name
        self.dim = dim
        self._ensure_collection()

    def _ensure_collection(self):
        qm = self._models
        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE),
            )

    def _ensure_dim(self, dim: int) -> bool:
        """远端与本地 embedding 维度可能不同（如 1024 vs 384）。

        集合为空时按实际向量维度重建；集合非空时维度不匹配说明 embedding
        提供方中途切换，跳过写入以保护已入库向量（SQLite 主记录不受影响）。
        """
        try:
            info = self.client.get_collection(self.collection_name)
            current = int(info.config.params.vectors.size)
            if current == dim:
                return True
            if self.count() > 0:
                logger.warning("Qdrant dim mismatch (%s != %s) with non-empty collection, skipping upsert",
                               current, dim)
                return False
            logger.info("Qdrant collection dim mismatch (%s -> %s), recreating empty collection", current, dim)
            self.client.delete_collection(self.collection_name)
        except Exception:
            pass
        self.dim = dim
        self._ensure_collection()
        return True

    def upsert(self, chunks: list[ChunkPayload], vectors: list[list[float]], *, keywords: list[str]):
        if not vectors:
            return
        if not self._ensure_dim(len(vectors[0])):
            return
        qm = self._models
        points = []
        for chunk, vector in zip(chunks, vectors):
            points.append(qm.PointStruct(
                id=chunk.chunk_key,
                vector=vector,
                payload={
                    "chunk_key": chunk.chunk_key,
                    "parent_chunk_key": chunk.parent_chunk_key,
                    "document_key": chunk.chunk_metadata.get("document_key", ""),
                    "title": chunk.title,
                    "source_name": chunk.source_name,
                    "source_type": "pdf",
                    "permission_level": "public",
                    "representation_level": chunk.representation_level,
                    "chunk_type": chunk.chunk_type,
                    "content": chunk.content,
                    "page_no": chunk.page_no,
                    "locator": chunk.locator,
                    "chunk_metadata": chunk.chunk_metadata,
                    "keywords": keywords,
                },
            ))
        for batch in _batches(points, 64):
            self.client.upsert(collection_name=self.collection_name, points=batch)

    def delete_document(self, document_key: str):
        from qdrant_client.http import models as qm
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=qm.FilterSelector(filter=qm.Filter(must=[
                qm.FieldCondition(key="document_key", match=qm.MatchValue(value=document_key)),
            ])),
        )

    def search(self, query_vector: list[float], *, top_k: int, product_id: str | None = None) -> list[RetrievedChunk]:
        qm = self._models
        # Parent summaries are already attached to matched child chunks by
        # RetrievalService. Searching them as peers returns the same content twice.
        filters = [qm.FieldCondition(key="representation_level", match=qm.MatchValue(value="fine"))]
        if product_id:
            filters.append(qm.FieldCondition(key="keywords", match=qm.MatchAny(any=[product_id])))
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            limit=top_k,
            with_payload=True,
            query_filter=qm.Filter(must=filters) if filters else None,
        )
        results: list[RetrievedChunk] = []
        for point in response.points:
            payload = point.payload or {}
            results.append(_payload_to_chunk(payload, point.id, float(point.score or 0.0), backend="qdrant"))
        return results

    def count(self) -> int:
        try:
            return self.client.count(collection_name=self.collection_name).count
        except Exception:
            return 0

    def exists(self) -> bool:
        return self.count() > 0


class KeywordStore:
    """ES 关键词召回（可选，带熔断）；ES 不可用时由检索服务回退 SQLite LIKE。"""

    def __init__(self, *, es_url: str, es_index: str, timeout: float, circuit_seconds: float):
        self.es_url = es_url
        self.es_index = es_index
        self.timeout = timeout
        self.circuit_seconds = circuit_seconds
        self._client = None
        if es_url:
            try:
                from elasticsearch import Elasticsearch
                self._client = Elasticsearch(hosts=[es_url], request_timeout=timeout,
                                             max_retries=0, retry_on_timeout=False)
            except Exception as exc:
                logger.warning("Elasticsearch client unavailable: %s", exc)

    def available(self) -> bool:
        return self._client is not None and not self._is_circuit_open()

    def index_chunks(self, chunks: list[ChunkPayload], *, keywords: list[str]):
        if self._client is None:
            return {"backend": "elasticsearch", "count": 0, "status": "skipped"}
        from elasticsearch.helpers import bulk
        actions = []
        for chunk in chunks:
            actions.append({"_index": self.es_index, "_id": chunk.chunk_key, "_source": {
                "chunk_key": chunk.chunk_key,
                "parent_chunk_key": chunk.parent_chunk_key,
                "document_key": chunk.chunk_metadata.get("document_key", ""),
                "title": chunk.title,
                "source_name": chunk.source_name,
                "source_type": "pdf",
                "permission_level": "public",
                "representation_level": chunk.representation_level,
                "chunk_type": chunk.chunk_type,
                "content": chunk.content,
                "page_no": chunk.page_no,
                "locator": chunk.locator,
                "chunk_metadata": chunk.chunk_metadata,
                "keywords": keywords,
            }})
        try:
            bulk(self._client, actions, refresh=False)
            return {"backend": "elasticsearch", "count": len(actions), "status": "indexed"}
        except Exception as exc:
            self._open_circuit()
            logger.warning("Elasticsearch indexing failed: %s", exc)
            return {"backend": "elasticsearch", "count": 0, "status": "failed"}

    def search(self, query: str, *, top_k: int, product_id: str | None = None) -> list[RetrievedChunk]:
        if self._client is None:
            return []
        try:
            must = [{"multi_match": {"query": query, "fields": ["content^3", "title^2", "source_name"]}}]
            # Parent summaries are context for a matching child, not independent
            # search results; querying both produces duplicate user-facing text.
            filters: list[dict] = [{"term": {"representation_level": "fine"}}]
            if product_id:
                filters.append({"term": {"keywords": product_id}})
            response = self._client.search(
                index=self.es_index,
                body={
                    "size": top_k,
                    "track_total_hits": False,
                    "timeout": f"{self.timeout:g}s",
                    "_source": ["chunk_key", "parent_chunk_key", "document_key", "title", "source_name",
                                "source_type", "permission_level", "representation_level", "chunk_type",
                                "content", "page_no", "locator", "chunk_metadata"],
                    "query": {"bool": {"must": must, "filter": filters}},
                },
            )
        except Exception as exc:
            self._open_circuit()
            logger.warning("Elasticsearch search failed, skipping keyword branch: %s", exc)
            return []
        self._close_circuit()
        results: list[RetrievedChunk] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            source["chunk_key"] = source.get("chunk_key", hit.get("_id"))
            results.append(_payload_to_chunk(source, hit.get("_id"), float(hit.get("_score") or 0.0), backend="elasticsearch"))
        return results

    def _is_circuit_open(self) -> bool:
        global _es_circuit_opened_at
        with _es_circuit_lock:
            opened_at = _es_circuit_opened_at
        if opened_at <= 0:
            return False
        return (time.monotonic() - opened_at) < self.circuit_seconds

    def _open_circuit(self) -> None:
        global _es_circuit_opened_at
        with _es_circuit_lock:
            _es_circuit_opened_at = time.monotonic()

    def _close_circuit(self) -> None:
        global _es_circuit_opened_at
        with _es_circuit_lock:
            _es_circuit_opened_at = 0.0


def _payload_to_chunk(payload: dict, point_id: str, score: float, *, backend: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_key=str(payload.get("chunk_key", point_id)),
        parent_chunk_key=str(payload["parent_chunk_key"]) if payload.get("parent_chunk_key") else None,
        document_key=str(payload.get("document_key", "")),
        title=str(payload.get("title", "")),
        source_name=str(payload.get("source_name", "")),
        source_type=str(payload.get("source_type", "")),
        permission_level=str(payload.get("permission_level", "public")),
        representation_level=str(payload.get("representation_level", "fine")),
        chunk_type=str(payload.get("chunk_type", "text")),
        content=str(payload.get("content", "")),
        page_no=int(payload["page_no"]) if payload.get("page_no") is not None else None,
        locator=str(payload["locator"]) if payload.get("locator") else None,
        chunk_metadata=payload.get("chunk_metadata", {}),
        score=score,
        backend=backend,
    )


def _batches(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]


class BM25Lite:
    """轻量 BM25 关键词召回（内存倒排索引），用于 SQLite 兜底前的本地关键词分支。

    无 ES 时使用 BM25 作为关键词检索分支，SQLite LIKE 作为最后兜底。
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, chunks: list[ChunkPayload]):
        self.chunks = {chunk.chunk_key: chunk for chunk in chunks}
        self.doc_lens = {chunk.chunk_key: max(1, len(chunk.content)) for chunk in chunks}
        self.avg_len = sum(self.doc_lens.values()) / max(1, len(self.doc_lens))
        self.postings: dict[str, Counter] = {}
        for chunk in chunks:
            tokens = set(self._tokenize(chunk.content))
            for token in tokens:
                self.postings.setdefault(token, Counter())[chunk.chunk_key] += 1
        self.doc_count = max(1, len(chunks))

    def search(self, query: str, *, top_k: int, product_id: str | None = None) -> list[RetrievedChunk]:
        query_tokens = Counter(self._tokenize(query))
        if not query_tokens:
            return []
        scores: dict[str, float] = {}
        for token, qtf in query_tokens.items():
            postings = self.postings.get(token, {})
            df = max(1, len(postings))
            idf = max(0.0, __import__("math").log(1 + (self.doc_count - df + 0.5) / (df + 0.5)))
            for doc_key, tf in postings.items():
                chunk = self.chunks[doc_key]
                if product_id and not any(keyword == product_id for keyword in chunk.chunk_metadata.get("keywords", [])):
                    continue
                norm_tf = (tf * (self.K1 + 1)) / (tf + self.K1 * (1 - self.B + self.B * self.doc_lens[doc_key] / self.avg_len))
                scores[doc_key] = scores.get(doc_key, 0.0) + idf * norm_tf * qtf
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
        results: list[RetrievedChunk] = []
        for doc_key, score in ranked:
            chunk = self.chunks[doc_key]
            results.append(_payload_to_chunk({
                "chunk_key": chunk.chunk_key,
                "parent_chunk_key": chunk.parent_chunk_key,
                "document_key": chunk.chunk_metadata.get("document_key", ""),
                "title": chunk.title,
                "source_name": chunk.source_name,
                "source_type": "pdf",
                "permission_level": "public",
                "representation_level": chunk.representation_level,
                "chunk_type": chunk.chunk_type,
                "content": chunk.content,
                "page_no": chunk.page_no,
                "locator": chunk.locator,
                "chunk_metadata": chunk.chunk_metadata,
            }, chunk.chunk_key, score, backend="bm25"))
        return results

    @staticmethod
    def _tokenize(text: str) -> list:
        import re
        normalized = " ".join(text.lower().split())
        word_tokens = re.findall(r"[a-z0-9_]+", normalized)
        cjk_chars = re.findall(r"[一-鿿]", normalized)
        cjk_bigrams = [normalized[index:index + 2] for index in range(max(len(normalized) - 1, 0))
                       if re.match(r"[一-鿿]{2}", normalized[index:index + 2])]
        return word_tokens + cjk_chars + cjk_bigrams
