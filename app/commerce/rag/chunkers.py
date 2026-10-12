# -*- coding: utf-8 -*-
"""分块策略：固定 / 递归语义 / 标题层级 / 政策条款 / 办理步骤 + 自动选择；表格专用分块；父子块构建。

- services/ingest/chunkers/text_chunker.py（五种策略 + 自动检测）
- services/ingest/chunkers/table_chunker.py（表头 + 数据行，不从中间截断）
- services/ingest/chunkers/parent_builder.py（按章节/页分组，LLM 摘要 + 本地兜底）
"""
from __future__ import annotations

import re
from uuid import uuid4

from app.commerce.rag.schemas import ChunkPayload, ParsedBlock

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except Exception:
    RecursiveCharacterTextSplitter = None


def _fixed_chunks(content: str, chunk_size: int, chunk_overlap: int) -> list:
    step = max(1, chunk_size - chunk_overlap)
    return [content[i:i + chunk_size] for i in range(0, len(content), step)]


def _recursive_chunks(content: str, chunk_size: int, chunk_overlap: int) -> list:
    if RecursiveCharacterTextSplitter is None:
        return _fixed_chunks(content, chunk_size, chunk_overlap)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", "。", "；", "，", " ", ""],
    )
    return splitter.split_text(content)


def _heading_chunks(content: str, chunk_size: int, chunk_overlap: int) -> list:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return []
    heading_pattern = re.compile(r"^(?:第[一二三四五六七八九十0-9]+[章节部分条]|[一二三四五六七八九十]+、|\d+[.、]|[A-Z][.、])")
    groups: list = []
    current: list = []
    for line in lines:
        if heading_pattern.match(line) and current:
            groups.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append("\n".join(current))
    chunks: list = []
    for group in groups:
        if len(group) > chunk_size:
            chunks.extend(_recursive_chunks(group, chunk_size, chunk_overlap))
        else:
            chunks.append(group)
    return chunks


def _policy_clause_chunks(content: str, chunk_size: int, chunk_overlap: int) -> list:
    parts = re.split(r"(?=(?:第[一二三四五六七八九十0-9]+条|第[一二三四五六七八九十0-9]+章|[一二三四五六七八九十]+、))", content)
    normalized = [part.strip() for part in parts if part.strip()]
    if not normalized:
        return _recursive_chunks(content, chunk_size, chunk_overlap)
    chunks: list = []
    for part in normalized:
        if len(part) > chunk_size:
            chunks.extend(_recursive_chunks(part, chunk_size, chunk_overlap))
        else:
            chunks.append(part)
    return chunks


def _procedure_step_chunks(content: str, chunk_size: int, chunk_overlap: int) -> list:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    step_pattern = re.compile(r"^(?:第[一二三四五六七八九十0-9]+步|步骤\s*\d+|\d+[.、]|[一二三四五六七八九十]+、)")
    steps: list = []
    current: list = []
    for line in lines:
        if step_pattern.match(line) and current:
            steps.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        steps.append("\n".join(current))
    if len(steps) <= 1:
        return _heading_chunks(content, chunk_size, chunk_overlap)
    chunks: list = []
    for step in steps:
        if len(step) > chunk_size:
            chunks.extend(_recursive_chunks(step, chunk_size, chunk_overlap))
        else:
            chunks.append(step)
    return chunks


def _detect_strategy(block: ParsedBlock, default: str) -> str:
    """按内容与来源文件名自动选择分块策略（电商口径）。"""
    if block.chunk_strategy:
        return block.chunk_strategy
    text = block.content
    source_name = block.source_name
    if block.block_type == "flowchart_mermaid":
        return "fixed"
    if re.search(r"(第一步|第二步|第三步|步骤\s*\d+)", text) or (
            re.search(r"(流程|办理|步骤|指引|指南|须知)", source_name) and re.search(r"(\d+[.、]|[一二三四五六七八九十]+、)", text)
    ):
        return "procedure_step"
    if re.search(r"(规则|政策|说明|办法|规定|FAQ|总览)", source_name) or re.search(r"(第[一二三四五六七八九十0-9]+条|第[一二三四五六七八九十0-9]+章)", text):
        return "policy_clause"
    if re.search(r"^(?:第[一二三四五六七八九十0-9]+[章节部分条]|[一二三四五六七八九十]+、|\d+[.、])", text, re.MULTILINE):
        return "heading"
    return default


def chunk_text_block(block: ParsedBlock, chunk_size: int, chunk_overlap: int, default_strategy: str = "auto") -> list:
    strategy = _detect_strategy(block, default_strategy)
    if strategy in {"auto", "semantic"}:
        chunks = _recursive_chunks(block.content, chunk_size, chunk_overlap)
    elif strategy == "fixed":
        chunks = _fixed_chunks(block.content, chunk_size, chunk_overlap)
    elif strategy == "heading":
        chunks = _heading_chunks(block.content, chunk_size, chunk_overlap)
    elif strategy == "policy_clause":
        chunks = _policy_clause_chunks(block.content, chunk_size, chunk_overlap)
    elif strategy == "procedure_step":
        chunks = _procedure_step_chunks(block.content, chunk_size, chunk_overlap)
    else:
        chunks = _recursive_chunks(block.content, chunk_size, chunk_overlap)

    payloads = []
    for index, chunk in enumerate(chunks, start=1):
        payloads.append(ChunkPayload(
            chunk_key=uuid4().hex,  # Qdrant 本地模式要求点 ID 为合法 UUID
            representation_level="fine",
            chunk_type=block.block_type,
            content=chunk,
            source_name=block.source_name,
            page_no=block.page_no,
            locator=block.locator,
            chunk_metadata={"block_id": block.block_id, "chunk_index": index, "chunk_strategy": strategy},
        ))
    return payloads


def chunk_table_block(block: ParsedBlock, chunk_size: int) -> list:
    """表格块：整体 ≤ chunk_size 时保留整表；超长时按行切分并重复表头，不从中间截断。"""
    lines = block.content.splitlines()
    header_end = None
    for index, line in enumerate(lines):
        if line.strip().startswith("| ---") or line.strip().startswith("|---"):
            header_end = index
            break
    if header_end is None:
        return chunk_text_block(block, chunk_size, chunk_size // 8)

    table_header = lines[:header_end + 1]
    body_lines = lines[header_end + 1:]
    chunks: list = []
    if len(block.content) <= chunk_size:
        chunks = [block.content]
    else:
        current: list = []
        current_len = len("\n".join(table_header))
        for line in body_lines:
            if current and current_len + len(line) + 1 > chunk_size:
                chunks.append("\n".join(table_header + current))
                current = []
                current_len = len("\n".join(table_header))
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append("\n".join(table_header + current))

    payloads = []
    for index, chunk in enumerate(chunks, start=1):
        payloads.append(ChunkPayload(
            chunk_key=uuid4().hex,
            representation_level="fine",
            chunk_type="table_markdown",
            content=chunk,
            source_name=block.source_name,
            page_no=block.page_no,
            locator=block.locator,
            chunk_metadata={"block_id": block.block_id, "chunk_index": index, "chunk_strategy": "table"},
        ))
    return payloads


# ======================== 父子块 ========================
def _fallback_parent_summary(children: list) -> str:
    summary_lines = []
    for child in children[:4]:
        snippet = " ".join(child.content.split())[:220]
        summary_lines.append(snippet)
    return "\n".join(summary_lines)


_SECTION_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百0-9]+[章节部分]|[一二三四五六七八九十]+、|\d+[.、])"
)


def _section_title(chunk: ChunkPayload):
    if chunk.chunk_type != "text":
        return None
    for line in chunk.content.splitlines()[:3]:
        normalized = line.strip()
        if normalized and _SECTION_HEADING.match(normalized):
            return normalized[:120]
    return None


def _group_children(child_chunks: list, max_children: int) -> list:
    """优先按章节分组；无标题时按页分组。"""
    groups: list = []
    current: list = []
    current_section = None
    fallback_page = None

    def flush():
        if not current:
            return
        label = current_section or f"page {fallback_page if fallback_page is not None else 'file'}"
        groups.append((label, list(current)))
        current.clear()

    for chunk in child_chunks:
        heading = _section_title(chunk)
        page_changed = fallback_page is not None and chunk.page_no != fallback_page
        if current and heading:
            flush()
            current_section = heading
            fallback_page = chunk.page_no
        elif current and current_section is None and page_changed:
            flush()
            fallback_page = chunk.page_no
        elif not current and heading:
            current_section = heading
            fallback_page = chunk.page_no
        elif not current and current_section is None:
            fallback_page = chunk.page_no
        current.append(chunk)
        if len(current) >= max_children:
            flush()
    flush()
    return groups


def build_parent_chunks(child_chunks: list, *, max_children: int = 6, llm_summarizer=None) -> list:
    """为子块生成页面/章节级父块摘要；llm_summarizer 不可用时本地截断兜底。"""
    grouped = _group_children(child_chunks, max(1, max_children))
    parent_chunks: list = []
    for group_index, (group_label, children) in enumerate(grouped, start=1):
        parent_key = uuid4().hex  # Qdrant 本地模式要求点 ID 为合法 UUID
        summary_method = "fallback_truncate"
        content = _fallback_parent_summary(children)
        if llm_summarizer is not None:
            try:
                llm_children = [
                    {"chunk_type": child.chunk_type, "locator": child.locator or "",
                     "content": child.content[:700]}
                    for child in children[:6]
                ]
                llm_summary = llm_summarizer(
                    source_name=children[0].source_name,
                    page_label=group_label,
                    children=llm_children,
                )
                if llm_summary:
                    content = llm_summary
                    summary_method = "llm_summary"
            except Exception:
                summary_method = "fallback_truncate"

        page_numbers = [child.page_no for child in children if child.page_no is not None]
        page_start = min(page_numbers) if page_numbers else None
        page_end = max(page_numbers) if page_numbers else None
        page_label = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
        scope = "section" if not group_label.startswith("page ") else "page"
        parent = ChunkPayload(
            chunk_key=parent_key,
            representation_level="coarse",
            chunk_type="parent_summary",
            content=content,
            source_name=children[0].source_name,
            page_no=page_start,
            locator=f"{scope} {group_label} | pages {page_label}",
            chunk_metadata={
                "child_count": len(children),
                "page_label": page_label,
                "parent_scope": scope,
                "section_title": group_label if scope == "section" else None,
                "group_index": group_index,
                "summary_method": summary_method,
            },
        )
        parent_chunks.append(parent)
        for child in children:
            child.parent_chunk_key = parent_key
    return parent_chunks
