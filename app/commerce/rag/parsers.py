# -*- coding: utf-8 -*-
"""PDF 解析：提取正文 / 表格 block，产出 ParsedDocument。

提取正文和表格，清理页眉页脚与噪声行，合并跨页表格并转换为 Markdown。
图片 OCR 与视觉摘要留作扩展点，当前知识库以 PDF 文档为主。
"""
from __future__ import annotations

import re
import os
import subprocess
import tempfile
import logging
import io
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import DefaultDict

import pdfplumber
import httpx

from app.commerce.rag.schemas import ParsedBlock, ParsedDocument

logger = logging.getLogger(__name__)

BBox = tuple
BODY_TOP_RATIO = 0.05
BODY_BOTTOM_RATIO = 0.95
MARGIN_TOP_SCAN_RATIO = 0.09
MARGIN_BOTTOM_SCAN_RATIO = 0.08
MULTI_COLUMN_GAP_RATIO = 0.08
MIN_COLUMN_WORDS = 25


@dataclass
class ExtractedTable:
    table_index: int
    page_start: int
    page_end: int
    header: list
    rows: list
    col_count: int
    header_signature: str
    start_bbox: BBox
    end_bbox: BBox
    start_page_height: float
    end_page_height: float


# ======================== 文本清洗 ========================
def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = text.replace("\r", "\n")
    text = text.replace("\t", " ")
    text = re.sub(r"(?<=[一-鿿])[a-z](?=[一-鿿])", "", text)
    text = re.sub(r"(?<=[、，。：；（(])[a-z](?=[一-鿿])", "", text)
    text = re.sub(r"[  ]+", " ", text)
    lines = []
    for raw_line in text.split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def regex_clean_narrative_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"([A-Za-z]{2,})-\s*\n\s*([A-Za-z]{2,})", r"\1\2", text)
    text = re.sub(r"(?<=[一-鿿])[a-z](?=[一-鿿])", "", text)
    text = re.sub(r"\[(?:\d+(?:\s*[-,]\s*\d+)*)\]", "", text)
    text = re.sub(r"\((?:\d+(?:\s*[-,]\s*\d+)*)\)", "", text)
    text = re.sub(r"\(cid:\d+\)", " ", text)
    text = clean_text(text)
    text = re.sub(r"(?<=\S)\n(?=\S)", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return clean_text(text)


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", clean_text(text))


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text))


def _is_page_number_line(text: str) -> bool:
    compact = _compact_text(text)
    return bool(re.fullmatch(r"[-–—_./()]*\d+[-–—_./()]*", compact))


def _normalize_margin_line(text: str) -> str:
    normalized = clean_text(text).strip(" -–—_./")
    if not normalized or _is_page_number_line(normalized):
        return ""
    return normalized


def _looks_like_noise_line(text: str) -> bool:
    compact = _compact_text(text)
    if not compact:
        return True
    if _is_page_number_line(compact):
        return True
    cjk_count = len(re.findall(r"[一-鿿]", compact))
    ascii_count = len(re.findall(r"[A-Za-z]", compact))
    digit_count = len(re.findall(r"\d", compact))
    punctuation_only = not re.search(r"[一-鿿A-Za-z0-9]", compact)
    if punctuation_only:
        return True
    if compact.count("(cid:") >= 2 and cjk_count == 0:
        return True
    if len(compact) <= 2 and cjk_count <= 1 and digit_count == 0:
        return True
    if len(compact) <= 4 and cjk_count == 0 and ascii_count <= 2 and digit_count <= 1:
        return True
    return False


def _looks_like_noise_word(word: dict) -> bool:
    compact = _compact_text(str(word.get("text", "")))
    if not compact:
        return True
    width = float(word.get("width", 0.0) or 0.0)
    height = float(word.get("height", 0.0) or 0.0)
    cjk_count = len(re.findall(r"[一-鿿]", compact))
    if compact.count("(cid:") >= 1 and cjk_count == 0:
        return True
    if re.fullmatch(r"[A-Za-z]", compact):
        return True
    if len(compact) == 1 and not re.search(r"[一-鿿A-Za-z0-9]", compact):
        return True
    if len(compact) <= 2 and max(width, height) >= 24:
        return True
    if len(compact) <= 2 and height > width * 3:
        return True
    return False


# ======================== 页眉页脚与正文提取 ========================
def _extract_margin_lines(page) -> list:
    page_width = float(page.width)
    page_height = float(page.height)
    regions = [
        (0.0, 0.0, page_width, page_height * MARGIN_TOP_SCAN_RATIO),
        (0.0, page_height * (1 - MARGIN_BOTTOM_SCAN_RATIO), page_width, page_height),
    ]
    lines = []
    for bbox in regions:
        text = page.crop(bbox).extract_text() or ""
        for line in clean_text(text).split("\n"):
            normalized = _normalize_margin_line(line)
            if normalized:
                lines.append(normalized)
    return lines


def _collect_repeated_margin_lines(pdf) -> set:
    counts: Counter = Counter()
    for page in pdf.pages:
        counts.update(_extract_margin_lines(page))
    min_hits = 2 if len(pdf.pages) <= 10 else 3
    return {line for line, count in counts.items() if count >= min_hits and len(line) <= 80}


def _word_overlaps_bboxes(word: dict, bboxes) -> bool:
    center_x = (float(word["x0"]) + float(word["x1"])) / 2
    center_y = (float(word["top"]) + float(word["bottom"])) / 2
    for bbox in bboxes:
        x0, top, x1, bottom = bbox
        if (x0 - 1.0) <= center_x <= (x1 + 1.0) and (top - 1.0) <= center_y <= (bottom + 1.0):
            return True
    return False


def _extract_page_words(page, exclusion_bboxes) -> list:
    words = page.extract_words(extra_attrs=["upright"]) or []
    body_top = float(page.height) * BODY_TOP_RATIO
    body_bottom = float(page.height) * BODY_BOTTOM_RATIO
    filtered = []
    for word in words:
        cleaned = clean_text(str(word.get("text", "")))
        if not cleaned:
            continue
        if not word.get("upright", True):
            continue
        if float(word["top"]) < body_top or float(word["bottom"]) > body_bottom:
            continue
        if _word_overlaps_bboxes(word, exclusion_bboxes):
            continue
        candidate = dict(word)
        candidate["text"] = cleaned
        if _looks_like_noise_word(candidate):
            continue
        filtered.append(candidate)
    return filtered


def _is_multi_column_layout(words: list, page_width: float) -> bool:
    if len(words) < MIN_COLUMN_WORDS * 2:
        return False
    band = max(24.0, page_width * MULTI_COLUMN_GAP_RATIO)
    mid = page_width / 2
    candidates = [word for word in words if len(_compact_text(str(word["text"]))) >= 2]
    left = [word for word in candidates if ((float(word["x0"]) + float(word["x1"])) / 2) < (mid - band)]
    right = [word for word in candidates if ((float(word["x0"]) + float(word["x1"])) / 2) > (mid + band)]
    center = [word for word in candidates if (mid - band) <= ((float(word["x0"]) + float(word["x1"])) / 2) <= (mid + band)]
    if len(left) < MIN_COLUMN_WORDS or len(right) < MIN_COLUMN_WORDS:
        return False
    return len(center) <= max(10, int(0.12 * (len(left) + len(right))))


def _render_words_as_text(words: list) -> str:
    if not words:
        return ""
    ordered = sorted(words, key=lambda item: (round(float(item["top"]), 1), float(item["x0"])))
    heights = sorted(float(item["height"]) for item in ordered if float(item.get("height", 0.0) or 0.0) > 0)
    median_height = heights[len(heights) // 2] if heights else 8.0
    line_threshold = max(3.0, min(12.0, median_height * 0.65))
    lines: list = []
    current_line: list = []
    current_top = 0.0
    for word in ordered:
        word_top = float(word["top"])
        if current_line and abs(word_top - current_top) > line_threshold:
            lines.append(current_line)
            current_line = [word]
            current_top = word_top
        else:
            current_line.append(word)
            if len(current_line) == 1:
                current_top = word_top
            else:
                current_top = (current_top * (len(current_line) - 1) + word_top) / len(current_line)
    if current_line:
        lines.append(current_line)
    rendered_lines = []
    for line_words in lines:
        line_words = sorted(line_words, key=lambda item: float(item["x0"]))
        fragments = [str(line_words[0]["text"])]
        previous = line_words[0]
        for word in line_words[1:]:
            gap = float(word["x0"]) - float(previous["x1"])
            needs_space = gap > max(2.5, min(float(previous["height"]), float(word["height"])) * 0.25)
            if needs_space and not (_contains_cjk(str(previous["text"])) and _contains_cjk(str(word["text"]))):
                fragments.append(" ")
            fragments.append(str(word["text"]))
            previous = word
        rendered_lines.append("".join(fragments))
    return clean_text("\n".join(rendered_lines))


def _postprocess_page_text(text: str, repeated_margin_lines: set) -> str:
    kept_lines = []
    previous = ""
    for line in clean_text(text).split("\n"):
        normalized = _normalize_margin_line(line)
        if normalized and normalized in repeated_margin_lines:
            continue
        if _looks_like_noise_line(line):
            continue
        if line == previous:
            continue
        kept_lines.append(line)
        previous = line
    return regex_clean_narrative_text("\n".join(kept_lines))


def _extract_clean_page_text(page, repeated_margin_lines: set, exclusion_bboxes) -> str:
    words = _extract_page_words(page, exclusion_bboxes)
    if not words:
        return ""
    page_width = float(page.width)
    if not _is_multi_column_layout(words, page_width):
        return _postprocess_page_text(_render_words_as_text(words), repeated_margin_lines)
    mid = page_width / 2
    spanning, left_words, right_words = [], [], []
    for word in words:
        center_x = (float(word["x0"]) + float(word["x1"])) / 2
        width = float(word["width"])
        if width > page_width * 0.55:
            spanning.append(word)
        elif center_x < mid:
            left_words.append(word)
        else:
            right_words.append(word)
    text_parts = [
        _render_words_as_text(spanning),
        _render_words_as_text(left_words),
        _render_words_as_text(right_words),
    ]
    combined = "\n".join(part for part in text_parts if part)
    return _postprocess_page_text(combined, repeated_margin_lines)


# ======================== 表格提取与 Markdown 化 ========================
def _normalize_table_rows(raw_rows) -> list:
    cleaned_rows = []
    max_cols = 0
    for row in raw_rows:
        normalized = [clean_text("" if cell is None else str(cell)) for cell in row]
        if any(normalized):
            cleaned_rows.append(normalized)
            max_cols = max(max_cols, len(normalized))
    if not cleaned_rows or max_cols == 0:
        return []
    return [row + [""] * (max_cols - len(row)) for row in cleaned_rows]


def _derive_table_header(rows) -> tuple:
    header = list(rows[0])
    data_rows = [list(row) for row in rows[1:]]
    if len(rows) >= 2:
        first_non_empty = sum(1 for cell in header if cell)
        second_non_empty = sum(1 for cell in rows[1] if cell)
        if first_non_empty <= max(1, len(header) // 2) and second_non_empty >= first_non_empty:
            merged = [clean_text(" ".join(part for part in (header[idx], rows[1][idx]) if part)) for idx in range(len(header))]
            if sum(1 for cell in merged if cell) >= second_non_empty:
                header = merged
                data_rows = [list(row) for row in rows[2:]]
    return header, data_rows


def _header_signature(header) -> str:
    normalized = [clean_text(cell) for cell in header if clean_text(cell)]
    return "|".join(normalized[:8])[:240]


def _build_table_candidate(table, page_num: int, page_height: float, table_index: int):
    rows = _normalize_table_rows(table.extract() or [])
    if len(rows) < 2:
        return None
    header, data_rows = _derive_table_header(rows)
    if not data_rows:
        return None
    col_count = max(len(header), max(len(row) for row in data_rows))
    normalized_header = header + [""] * (col_count - len(header))
    normalized_rows = [normalized_header] + [row + [""] * (col_count - len(row)) for row in data_rows]
    bbox = tuple(float(value) for value in table.bbox)
    return ExtractedTable(
        table_index=table_index, page_start=page_num, page_end=page_num,
        header=normalized_header, rows=normalized_rows, col_count=col_count,
        header_signature=_header_signature(normalized_header),
        start_bbox=bbox, end_bbox=bbox,
        start_page_height=page_height, end_page_height=page_height,
    )


def _should_merge_tables(previous: ExtractedTable, current: ExtractedTable) -> bool:
    if current.page_start != previous.page_end + 1:
        return False
    if abs(previous.col_count - current.col_count) > 1:
        return False
    if not previous.header_signature or not current.header_signature:
        return False
    similarity = SequenceMatcher(None, previous.header_signature, current.header_signature).ratio()
    previous_bottom_ratio = previous.end_bbox[3] / max(previous.end_page_height, 1.0)
    current_top_ratio = current.start_bbox[1] / max(current.start_page_height, 1.0)
    return similarity >= 0.92 or (similarity >= 0.82 and previous_bottom_ratio >= 0.58 and current_top_ratio <= 0.42)


def _merge_table_candidates(previous: ExtractedTable, current: ExtractedTable) -> None:
    current_rows = current.rows
    header_similarity = SequenceMatcher(None, previous.header_signature, current.header_signature).ratio()
    if header_similarity >= 0.82 and current_rows:
        current_rows = current_rows[1:]
    previous.rows.extend(current_rows)
    previous.page_end = current.page_end
    previous.end_bbox = current.end_bbox
    previous.end_page_height = current.end_page_height


def _unique_column_names(header, col_count: int) -> list:
    seen: Counter = Counter()
    columns = []
    for index in range(col_count):
        raw_name = clean_text(header[index]) if index < len(header) else ""
        name = raw_name or f"column_{index + 1}"
        seen[name] += 1
        if seen[name] > 1:
            name = f"{name}_{seen[name]}"
        columns.append(name)
    return columns


def _rows_to_markdown(columns, rows, limit: int = 8) -> str:
    limited_rows = [list(row)[:len(columns)] for row in rows[:limit]]
    header_row = "| " + " | ".join(columns) + " |"
    separator_row = "| " + " | ".join(["---"] * len(columns)) + " |"
    body_rows = ["| " + " | ".join(clean_text("" if cell is None else str(cell)) for cell in row) + " |"
                 for row in limited_rows]
    return "\n".join([header_row, separator_row, *body_rows])


def _table_to_markdown(table: ExtractedTable) -> str:
    columns = _unique_column_names(table.header, table.col_count)
    data_rows = [row[:table.col_count] for row in table.rows[1:]]
    page_label = str(table.page_start) if table.page_start == table.page_end else f"{table.page_start}-{table.page_end}"
    return (
        f"表格（第 {page_label} 页，共 {len(data_rows)} 行 {len(columns)} 列，列名：{', '.join(columns)}）：\n"
        f"{_rows_to_markdown(columns, data_rows)}"
    )


def _extract_pdf_tables(pdf) -> tuple:
    merged_tables: list = []
    table_bboxes_by_page: DefaultDict = defaultdict(list)
    for page_num, page in enumerate(pdf.pages, start=1):
        page_height = float(page.height)
        page_tables = page.find_tables()
        if not page_tables:
            continue
        for index, table in enumerate(page_tables, start=1):
            candidate = _build_table_candidate(table, page_num, page_height, index)
            if candidate is None:
                continue
            table_bboxes_by_page[page_num].append(candidate.start_bbox)
            if merged_tables and _should_merge_tables(merged_tables[-1], candidate):
                _merge_table_candidates(merged_tables[-1], candidate)
            else:
                merged_tables.append(candidate)
    return merged_tables, table_bboxes_by_page


# ======================== 解析入口 ========================
def _extract_keywords(text: str) -> list:
    return sorted(set(re.findall(r"\bP\d{3}\b", text.upper())))


def parse_pdf(*, document_key: str, source_name: str, file_path: Path,
              parser: str | None = None, mineru_timeout_seconds: float | None = None,
              mineru_api_url: str | None = None, mineru_api_key: str | None = None,
              mineru_model: str | None = None) -> ParsedDocument:
    """PDF 解析入口。可选优先调用 MinerU CLI，失败或未安装时回退 pdfplumber。"""
    selected_parser = (parser or os.getenv("COMMERCE_RAG_PARSER", "pdfplumber")).strip().lower()
    if selected_parser in {"mineru_api", "mineru_online"}:
        parsed = _parse_pdf_with_mineru_api(
            document_key=document_key, source_name=source_name, file_path=file_path,
            api_url=mineru_api_url or os.getenv("COMMERCE_RAG_MINERU_API_URL", "https://mineru.net/api/v4"),
            api_key=mineru_api_key or os.getenv("COMMERCE_RAG_MINERU_API_KEY", ""),
            model=mineru_model or os.getenv("COMMERCE_RAG_MINERU_MODEL", "vlm"),
            timeout_seconds=mineru_timeout_seconds,
        )
        if parsed is not None and parsed.blocks:
            return parsed
    if selected_parser == "mineru":
        parsed = _parse_pdf_with_mineru(document_key=document_key, source_name=source_name,
                                        file_path=file_path,
                                        timeout_seconds=mineru_timeout_seconds)
        if parsed is not None and parsed.blocks:
            return parsed
    return _parse_pdf_with_pdfplumber(document_key=document_key, source_name=source_name,
                                      file_path=file_path)


def _parse_pdf_with_mineru_api(*, document_key: str, source_name: str, file_path: Path,
                               api_url: str, api_key: str, model: str,
                               timeout_seconds: float | None = None) -> ParsedDocument | None:
    """Submit local PDF to MinerU's cloud API, poll completion, and parse full.md from ZIP."""
    if not api_key:
        logger.warning("MinerU online API key missing; falling back to pdfplumber for %s", source_name)
        return None
    timeout = timeout_seconds or 300.0
    base_url = api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    phase = "requesting upload URL"
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=30.0), follow_redirects=True) as client:
            # Request a short-lived object-storage URL, then upload file bytes directly to it.
            response = client.post(f"{base_url}/file-urls/batch", headers=headers, json={
                "files": [{"name": file_path.name, "data_id": document_key}],
                "model_version": model,
                "enable_table": True,
                "language": "ch",
            })
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"upload URL request failed: {payload.get('msg', 'unknown error')}")
            data = payload.get("data") or {}
            batch_id = data.get("batch_id")
            upload_urls = data.get("file_urls") or []
            if not batch_id or not upload_urls:
                raise RuntimeError("MinerU returned no batch_id or upload URL")
            # Presigned URLs carry their own credentials. Never send MinerU bearer key to object storage.
            phase = "uploading PDF to MinerU object storage"
            with file_path.open("rb") as pdf_file:
                # Keep the signed URL's upload contract intact; MinerU's example uses a bare PUT.
                upload = client.put(upload_urls[0], content=pdf_file)
                upload.raise_for_status()

            deadline = time.monotonic() + timeout
            result = None
            phase = "polling MinerU parse job"
            while time.monotonic() < deadline:
                poll = client.get(f"{base_url}/extract-results/batch/{batch_id}", headers=headers)
                poll.raise_for_status()
                poll_payload = poll.json()
                if poll_payload.get("code") != 0:
                    raise RuntimeError(f"MinerU poll failed: {poll_payload.get('msg', 'unknown error')}")
                extract_results = (poll_payload.get("data") or {}).get("extract_result") or []
                if isinstance(extract_results, dict):
                    extract_results = [extract_results]
                if extract_results:
                    result = extract_results[0]
                    state = result.get("state")
                    phase = f"polling MinerU job {batch_id} (state={state or 'unknown'})"
                    if state == "done":
                        break
                    if state == "failed":
                        raise RuntimeError(f"MinerU parse failed: {result.get('err_msg', 'unknown error')}")
                time.sleep(2)
            else:
                raise TimeoutError(f"MinerU parse job exceeded {timeout:g}s")

            zip_url = result.get("full_zip_url") if result else None
            if not zip_url:
                raise RuntimeError("MinerU completed without full_zip_url")
            # Result URL is a signed download URL; do not forward API credentials.
            phase = "downloading MinerU result archive"
            archive_response = client.get(zip_url)
            archive_response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
                md_names = [name for name in archive.namelist()
                            if Path(name).name.lower() in {"full.md", "document.md"}
                            or name.lower().endswith(".md")]
                if not md_names:
                    raise RuntimeError("MinerU output archive contains no Markdown")
                md_name = next((name for name in md_names if Path(name).name.lower() == "full.md"), md_names[0])
                markdown = archive.read(md_name).decode("utf-8", errors="replace")
            blocks = _mineru_markdown_blocks(markdown, source_name)
            if not blocks:
                raise RuntimeError("MinerU Markdown contained no usable text")
            return ParsedDocument(document_key=document_key, source_type="pdf", source_name=source_name,
                source_path=str(file_path), title=Path(source_name).stem, blocks=blocks,
                keywords=_extract_keywords("\n".join(block.content for block in blocks)))
    except Exception as exc:
        # Avoid logging request URLs: signed object-storage URLs contain temporary credentials.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        logger.warning("MinerU online parse failed for %s during %s (%s); falling back to pdfplumber",
                       source_name, phase, detail)
        return None


def _parse_pdf_with_mineru(*, document_key: str, source_name: str,
                           file_path: Path, timeout_seconds: float | None = None) -> ParsedDocument | None:
    """通过 MinerU 命令行生成 Markdown；保持原文档入库模型不变。"""
    output_dir = None
    try:
        with tempfile.TemporaryDirectory(prefix="commerce-mineru-") as temp_dir:
            output_dir = Path(temp_dir) / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            markdown_path = output_dir / "document.md"
            timeout = timeout_seconds or float(os.getenv("COMMERCE_RAG_MINERU_TIMEOUT_SECONDS", "300"))
            commands = [
                # Current MinerU document-library CLI; --pages all avoids its first-10-pages default.
                ["mineru", "parse", str(file_path.resolve()), "--pages", "all", "-o", str(markdown_path)],
                # Stateless CLI available in recent installations.
                ["mineru-kit", "parse", str(file_path.resolve()), "-o", str(markdown_path)],
                # Legacy MinerU CLI.
                ["mineru", "-p", str(file_path.resolve()), "-o", str(output_dir)],
            ]
            completed = None
            for command in commands:
                try:
                    attempt = subprocess.run(command, capture_output=True, text=True,
                                             timeout=timeout, check=False)
                except FileNotFoundError:
                    continue
                if attempt.returncode == 0:
                    completed = attempt
                    break
            if completed is None:
                logger.warning("MinerU CLI unavailable or failed for %s", source_name)
                return None
            markdown_files = ([markdown_path] if markdown_path.is_file()
                              else sorted(output_dir.rglob("*.md")))
            if not markdown_files:
                logger.warning("MinerU produced no Markdown for %s", source_name)
                return None
            markdown = "\n\n".join(path.read_text(encoding="utf-8", errors="replace")
                                     for path in markdown_files)
            blocks = _mineru_markdown_blocks(markdown, source_name)
            if not blocks:
                logger.warning("MinerU Markdown was empty for %s", source_name)
                return None
            title = Path(source_name).stem
            return ParsedDocument(
                document_key=document_key, source_type="pdf", source_name=source_name,
                source_path=str(file_path), title=title, blocks=blocks,
                keywords=_extract_keywords("\n".join(block.content for block in blocks)),
            )
    except subprocess.TimeoutExpired:
        logger.warning("MinerU timed out for %s; falling back to pdfplumber", source_name)
    except Exception as exc:
        logger.warning("MinerU parse failed for %s: %s; falling back to pdfplumber", source_name, exc)
    return None


def _mineru_markdown_blocks(markdown: str, source_name: str) -> list[ParsedBlock]:
    """将 MinerU Markdown 按页和表格拆成项目统一的解析 block。"""
    blocks: list[ParsedBlock] = []
    page_no = 1
    text_lines: list[str] = []
    table_lines: list[str] = []
    in_table = False

    def flush_text():
        content = clean_text("\n".join(text_lines))
        if content:
            blocks.append(ParsedBlock(block_id=f"mineru-text-{page_no}-{len(blocks)}",
                block_type="text", content=content, page_no=page_no,
                locator=f"page {page_no}", source_name=source_name,
                structured_payload={"parser": "mineru"}))
        text_lines.clear()

    def flush_table():
        if table_lines:
            content = "\n".join(line.rstrip() for line in table_lines).strip()
            if content:
                blocks.append(ParsedBlock(block_id=f"mineru-table-{page_no}-{len(blocks)}",
                    block_type="table_markdown", content=content, page_no=page_no,
                    locator=f"page {page_no} | MinerU table", source_name=source_name,
                    structured_payload={"table_format": "markdown", "parser": "mineru"}))
        table_lines.clear()

    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        page_match = re.match(r"^\s*<!--\s*page(?:_?no)?\s*[:= ]\s*(\d+)\s*-->\s*$", line, re.I)
        if page_match:
            if in_table:
                flush_table()
                in_table = False
            flush_text()
            page_no = int(page_match.group(1))
            continue
        is_table_separator = "|" in line and bool(re.fullmatch(r"\s*\|?[-:| ]+\|?\s*", line))
        next_is_table_separator = (index + 1 < len(lines) and "|" in line and "|" in lines[index + 1]
                                   and bool(re.fullmatch(r"\s*\|?[-:| ]+\|?\s*", lines[index + 1])))
        if is_table_separator or next_is_table_separator:
            if not in_table:
                flush_text()
            in_table = True
        elif in_table and "|" not in line:
            flush_table()
            in_table = False
        (table_lines if in_table else text_lines).append(line)
    if in_table:
        flush_table()
    flush_text()
    return blocks


def _parse_pdf_with_pdfplumber(*, document_key: str, source_name: str,
                               file_path: Path) -> ParsedDocument:
    blocks: list = []
    warnings: list = []
    with pdfplumber.open(str(file_path)) as pdf:
        repeated_margin_lines = _collect_repeated_margin_lines(pdf)
        merged_tables, table_bboxes_by_page = _extract_pdf_tables(pdf)
        tables_by_start_page: DefaultDict = defaultdict(list)
        for table in merged_tables:
            tables_by_start_page[table.page_start].append(table)

        for page_num, page in enumerate(pdf.pages, start=1):
            exclusion_bboxes = list(table_bboxes_by_page.get(page_num, []))
            text = _extract_clean_page_text(page, repeated_margin_lines, exclusion_bboxes)
            if text:
                blocks.append(ParsedBlock(
                    block_id=f"text-{page_num}", block_type="text", content=text,
                    page_no=page_num, locator=f"page {page_num}", source_name=source_name,
                ))
            else:
                warnings.append(f"page {page_num} extracted empty text")

            for table_index, table in enumerate(tables_by_start_page.get(page_num, []), start=1):
                markdown = _table_to_markdown(table)
                page_label = str(table.page_start) if table.page_start == table.page_end else f"{table.page_start}-{table.page_end}"
                blocks.append(ParsedBlock(
                    block_id=f"table-{page_label}-{table_index}", block_type="table_markdown",
                    content=markdown, page_no=table.page_start,
                    locator=f"pages {page_label} | table {table.table_index}",
                    source_name=source_name,
                    structured_payload={
                        "page_end": table.page_end,
                        "table_index": table.table_index,
                        "table_format": "markdown",
                    },
                ))

    title = source_name
    if title.lower().endswith(".pdf"):
        title = title[:-4]
    return ParsedDocument(
        document_key=document_key,
        source_type="pdf",
        source_name=source_name,
        source_path=str(file_path),
        title=title,
        blocks=blocks,
        warnings=warnings,
        keywords=_extract_keywords("\n".join(block.content for block in blocks)),
    )
