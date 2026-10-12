"""Run a single-file MinerU online parsing smoke test without console encoding issues."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.commerce.config import Settings
from app.commerce.rag.parsers import parse_pdf


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test MinerU PDF parsing on one local file")
    parser.add_argument("pdf", nargs="?", default="", help="PDF path; defaults to the P100 demo PDF")
    parser.add_argument("--timeout", type=float, default=600, help="Maximum MinerU wait in seconds (default: 600)")
    parser.add_argument("--model", choices=("pipeline", "vlm"), help="Override the configured MinerU model")
    args = parser.parse_args()

    pdf_path = Path(args.pdf) if args.pdf else Path(__file__).resolve().parents[1] / "raw_data" / "pdf" / "商品说明_轻享无线耳机P100.pdf"
    if not pdf_path.is_file():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    settings = Settings()
    if settings.rag_parser not in {"mineru_api", "mineru_online"}:
        print(f"COMMERCE_RAG_PARSER is {settings.rag_parser!r}; set it to 'mineru_api' in .env first.")
        return 2
    if not settings.rag_mineru_api_key:
        print("COMMERCE_RAG_MINERU_API_KEY is empty; add the MinerU API token to .env.")
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parsed = parse_pdf(
        document_key="mineru-smoke-test",
        source_name=pdf_path.name,
        file_path=pdf_path,
        parser=settings.rag_parser,
        mineru_timeout_seconds=args.timeout,
        mineru_api_url=settings.rag_mineru_api_url,
        mineru_api_key=settings.rag_mineru_api_key,
        mineru_model=args.model or settings.rag_mineru_model,
    )
    online_blocks = [block for block in parsed.blocks
                     if block.structured_payload.get("parser") == "mineru"]
    print(f"PDF: {pdf_path.name}")
    print(f"Blocks: {len(parsed.blocks)}")
    print(f"Product IDs: {', '.join(parsed.keywords) or '(none)'}")
    if online_blocks:
        print("Result: MINERU_ONLINE_OK")
        print("Sample: " + online_blocks[0].content[:200])
        return 0

    print("Result: MINERU_ONLINE_FAILED_OR_FALLBACK")
    print("The parsed blocks came from pdfplumber fallback, not MinerU online.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
