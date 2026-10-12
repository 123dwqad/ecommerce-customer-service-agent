# -*- coding: utf-8 -*-
"""电商 RAG 文档、文本块与检索结果的数据对象。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ParsedBlock(BaseModel):
    block_id: str
    block_type: str  # text / table_markdown / image_ocr_text / image_summary / flowchart_mermaid
    content: str
    page_no: int | None = None
    locator: str | None = None
    source_name: str = ""
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    chunk_strategy: str | None = None


class ParsedDocument(BaseModel):
    document_key: str
    source_type: str = "pdf"
    source_name: str
    source_path: str = ""
    title: str = ""
    blocks: list[ParsedBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # 文档内出现的商品编号 P\d{3}


class ChunkPayload(BaseModel):
    chunk_key: str
    parent_chunk_key: str | None = None
    representation_level: str = "fine"  # fine=子块 / coarse=父块
    chunk_type: str = "text"
    content: str
    title: str = ""
    source_name: str = ""
    page_no: int | None = None
    locator: str | None = None
    chunk_metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievedChunk(BaseModel):
    chunk_key: str
    parent_chunk_key: str | None = None
    document_key: str = ""
    title: str = ""
    source_name: str = ""
    source_type: str = ""
    permission_level: str = "public"
    representation_level: str = "fine"
    chunk_type: str = "text"
    content: str = ""
    page_no: int | None = None
    locator: str | None = None
    chunk_metadata: dict[str, Any] = Field(default_factory=dict)
    score: float = 0.0
    backend: str = ""
    parent_content: str | None = None
    parent_locator: str | None = None


class RetrievalPlan(BaseModel):
    route: str = "hybrid"  # keyword / vector / hybrid
    rewritten_query: str = ""
    step_back_query: str | None = None
    es_available: bool = False
    qdrant_available: bool = False
    rerank_available: bool = False
    es_candidate_count: int = 0
    qdrant_candidate_count: int = 0
    fused_count: int = 0
    rerank_candidate_count: int = 0
    rerank_applied: bool = False
    fallback_backend: str | None = None
    parent_context_count: int = 0
