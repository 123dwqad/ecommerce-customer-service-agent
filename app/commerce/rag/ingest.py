# -*- coding: utf-8 -*-
"""电商知识库文档入库管道。

流程：解析 PDF → 分 block → 多策略切分 child chunks → 构建 parent chunks
→ 写入 SQLite（主记录，支持 LIKE 兜底检索）→ 写入 Qdrant（向量）
→ 写入 ES（可选，关键词）。支持按文件 mtime 增量重建单篇文档。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy import delete, select

from app.commerce.rag.chunkers import build_parent_chunks, chunk_table_block, chunk_text_block
from app.commerce.rag.parsers import parse_pdf
from app.commerce.rag.schemas import ChunkPayload, ParsedDocument

logger = logging.getLogger(__name__)


def document_key_for(source_name: str) -> str:
    digest = hashlib.md5(source_name.encode("utf-8")).hexdigest()[:12]
    return f"doc-{digest}"


class IngestService:
    def __init__(self, *, store, qdrant_store, keyword_store, embedder, settings, rewriter=None):
        from app.commerce.db_models import RagChunk, RagDocument
        self.store = store
        self.qdrant = qdrant_store
        self.keyword = keyword_store
        self.embedder = embedder
        self.settings = settings
        self.rewriter = rewriter
        self.RagChunk = RagChunk
        self.RagDocument = RagDocument
        self._building = False

    # ---------------- 构建 ----------------
    def build_all(self, *, force: bool = False) -> dict:
        """扫描 raw_data 目录，增量重建新增/修改的文档，清理已删除的文档。"""
        data_dir = Path(self.settings.rag_data_dir)
        pdf_files = sorted(data_dir.glob("*.pdf")) if data_dir.exists() else []
        changed: list = []
        skipped: list = []
        summaries: list = []
        with self.store.session.begin() as db:
            existing = {doc.document_key: doc for doc in db.scalars(select(self.RagDocument))}
            for file_path in pdf_files:
                key = document_key_for(file_path.name)
                mtime = file_path.stat().st_mtime
                record = existing.get(key)
                if record is not None and abs(record.file_mtime - mtime) < 0.001 and not force:
                    skipped.append(record.source_name)
                    summaries.append(self._summary_dict(record))
                    continue
                if record is not None:
                    self._drop_document(db, record.document_key)
                else:
                    # 清理库外残留点（如历史构建遗留），保证重复构建幂等
                    self.qdrant.delete_document(key)
                summary = self._build_one(db, file_path, key, mtime)
                changed.append(file_path.name)
                summaries.append(summary)
            deleted = [doc for key, doc in existing.items() if key not in {document_key_for(f.name) for f in pdf_files}]
            for doc in deleted:
                self._drop_document(db, doc.document_key)
        return {
            "status": "ok",
            "total": len(pdf_files),
            "changed": changed,
            "deleted": [doc.source_name for doc in deleted],
            "skipped": skipped,
            "documents": summaries,
            "backend_status": self.backend_status(),
        }

    def _build_one(self, db, file_path: Path, key: str, mtime: float) -> dict:
        from app.commerce.db_models import RagDocument
        settings = self.settings
        parsed = parse_pdf(document_key=key, source_name=file_path.name, file_path=file_path,
                           parser=getattr(self.settings, "rag_parser", "pdfplumber"),
                           mineru_timeout_seconds=getattr(self.settings, "rag_mineru_timeout_seconds", 300),
                           mineru_api_url=getattr(self.settings, "rag_mineru_api_url", None),
                           mineru_api_key=getattr(self.settings, "rag_mineru_api_key", None),
                           mineru_model=getattr(self.settings, "rag_mineru_model", None))
        child_chunks: list[ChunkPayload] = []
        for block in parsed.blocks:
            if block.block_type in {"text", "flowchart_mermaid"}:
                child_chunks.extend(chunk_text_block(block, settings.rag_chunk_size, settings.rag_chunk_overlap))
            elif block.block_type == "table_markdown":
                child_chunks.extend(chunk_table_block(block, settings.rag_chunk_size))
            else:
                child_chunks.extend(chunk_text_block(block, settings.rag_chunk_size, settings.rag_chunk_overlap))
        parent_chunks = build_parent_chunks(child_chunks, max_children=6, llm_summarizer=self._llm_parent_summary)
        all_chunks = [*parent_chunks, *child_chunks]
        for chunk in all_chunks:
            chunk.title = parsed.title
            chunk.chunk_metadata["document_key"] = key

        # SQLite 主记录
        db.add(RagDocument(
            document_key=key, title=parsed.title, source_name=parsed.source_name,
            source_path=str(file_path), file_mtime=mtime, status="indexed",
            child_chunk_count=len(child_chunks), parent_chunk_count=len(parent_chunks),
            keywords=parsed.keywords, warnings=parsed.warnings,
        ))
        db.flush()
        for chunk in all_chunks:
            db.add(self.RagChunk(
                chunk_key=chunk.chunk_key, document_key=key,
                parent_chunk_key=chunk.parent_chunk_key,
                representation_level=chunk.representation_level,
                chunk_type=chunk.chunk_type, title=chunk.title,
                source_name=chunk.source_name, content=chunk.content[:4000],
                page_no=chunk.page_no, locator=chunk.locator,
                chunk_metadata=chunk.chunk_metadata,
            ))

        # 向量 + 关键词索引
        vectors = self.embedder.embed_documents([chunk.content for chunk in all_chunks])
        self.qdrant.upsert(all_chunks, vectors, keywords=parsed.keywords)
        self.keyword.index_chunks(all_chunks, keywords=parsed.keywords)
        return {
            "document_key": key, "title": parsed.title, "source_name": parsed.source_name,
            "child_chunks": len(child_chunks), "parent_chunks": len(parent_chunks),
            "warnings": parsed.warnings, "keywords": parsed.keywords,
        }

    def _drop_document(self, db, document_key: str):
        """复用外层会话删除记录，避免 SQLite 并发写锁；Qdrant 删除独立执行。"""
        self.qdrant.delete_document(document_key)
        db.execute(delete(self.RagChunk).where(self.RagChunk.document_key == document_key))
        db.execute(delete(self.RagDocument).where(self.RagDocument.document_key == document_key))

    @staticmethod
    def _summary_dict(record) -> dict:
        return {
            "document_key": record.document_key, "title": record.title,
            "source_name": record.source_name,
            "child_chunks": record.child_chunk_count, "parent_chunks": record.parent_chunk_count,
            "warnings": record.warnings or [], "keywords": record.keywords or [],
        }

    def _llm_parent_summary(self, *, source_name: str, page_label: str, children: list[dict]) -> str | None:
        if self.rewriter is None or not self.rewriter.is_available():
            return None
        lines = [f"- [{child['chunk_type']}] {child['locator']}: {child['content'][:120]}" for child in children]
        prompt = (
            "请为以下电商知识文档的一节内容生成 1-2 句中文摘要，"
            "保留商品编号、规格参数、规则条件、数字和办理步骤关键词。只输出摘要。\n\n"
            f"文档：{source_name}\n章节：{page_label}\n内容：\n" + "\n".join(lines)
        )
        try:
            return self.rewriter._complete(prompt, max_tokens=160)
        except Exception:
            return None

    # ---------------- 状态 ----------------
    def backend_status(self) -> dict:
        return {
            "qdrant": {"count": self.qdrant.count()},
            "keyword": {"es_available": self.keyword.available()},
        }

    def ensure_index(self) -> dict | None:
        """索引为空（首次使用）时自动构建；构建期间并发调用只触发一次。"""
        if self._building:
            return None
        with self.store.session() as db:
            doc_count = len(db.scalars(select(self.RagDocument)).all())
        if doc_count > 0 and self.qdrant.exists():
            return None
        self._building = True
        try:
            return self.build_all(force=False)
        finally:
            self._building = False
