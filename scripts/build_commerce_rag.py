# -*- coding: utf-8 -*-
# build_commerce_rag.py
# 构建电商客服 RAG 索引：解析 raw_data/pdf/*.pdf → 多策略切块 → 父子块
# → SQLite + Qdrant（+ 可选 ES）。
#
# 用法:
#   .\.venv\Scripts\python.exe -m scripts.build_commerce_rag            # 增量（只重建变更文档）
#   .\.venv\Scripts\python.exe -m scripts.build_commerce_rag --rebuild  # 全量重建
#   .\.venv\Scripts\python.exe -m scripts.build_commerce_rag --ask "包邮规则"  # 冒烟检索
from __future__ import annotations

import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(description="Build the commerce RAG index from raw_data/pdf")
    parser.add_argument("--rebuild", action="store_true", help="force full rebuild")
    parser.add_argument("--ask", default="", help="smoke query after build")
    args = parser.parse_args()

    from app.commerce.config import Settings
    from app.commerce.rag.service import RAGService
    from app.commerce.store import Store

    settings = Settings()
    store = Store(settings.database_url)
    rag = RAGService(store, settings)

    result = rag.build(force=args.rebuild)
    print(json.dumps({k: v for k, v in result.items() if k != "documents"}, ensure_ascii=False, indent=2))
    for doc in result.get("documents", []):
        print(f"  {doc['source_name']}: child={doc['child_chunks']} parent={doc['parent_chunks']} "
              f"keywords={doc['keywords']} warnings={doc['warnings']}")

    if args.ask:
        response = rag.search(query=args.ask)
        print("\nquery:", args.ask)
        print("plan:", json.dumps(response["plan"], ensure_ascii=False, indent=2))
        for hit in response["hits"]:
            print(f"  [{hit['backend']} {hit['score']}] {hit['title']} | {hit['locator']}")
            print(f"    {hit['text'][:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
