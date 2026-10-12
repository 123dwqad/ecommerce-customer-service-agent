# -*- coding: utf-8 -*-
"""RAG 链路测试：分块策略 / 父子块 / 入库检索 / 改写降级 / 工具 Schema。

使用本地哈希 embedding（不依赖外部 Key）与临时索引目录，全程离线。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.commerce.rag.chunkers import build_parent_chunks, chunk_table_block, chunk_text_block
from app.commerce.rag.parsers import parse_pdf
from app.commerce.rag.schemas import ParsedBlock
from app.commerce.rag.stores import BM25Lite

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "raw_data" / "pdf"


class ChunkerTests(unittest.TestCase):
    def test_policy_clause_strategy_on_rule_doc(self):
        block = ParsedBlock(block_id="b1", block_type="text", source_name="店铺服务_发货时效与配送说明.pdf",
                            content="一、发货时效\n普通现货商品付款后 48 小时内发货。\n定制商品付款后 120 小时内发货。")
        chunks = chunk_text_block(block, 800, 100)
        self.assertGreaterEqual(len(chunks), 1)
        # 文件名含"说明"，且无步骤标记 → policy_clause 策略
        self.assertEqual(chunks[0].chunk_metadata["chunk_strategy"], "policy_clause")
        self.assertEqual(chunks[0].representation_level, "fine")
        self.assertTrue(all(len(chunk.chunk_key) == 32 for chunk in chunks))

    def test_procedure_step_strategy_on_flow_doc(self):
        block = ParsedBlock(block_id="b1", block_type="text", source_name="售后流程_退换货办理说明.pdf",
                            content="1. 进入订单页选择售后\n2. 选择退货原因并提交申请\n3. 等待审核")
        chunks = chunk_text_block(block, 800, 100)
        # 文件名含"流程"且存在编号步骤 → procedure_step 策略
        self.assertEqual(chunks[0].chunk_metadata["chunk_strategy"], "procedure_step")

    def test_table_chunker_keeps_header_when_splitting(self):
        content = ("表格（第 1 页，共 40 行 2 列）：\n| 配送区域 | 运费规则 |\n| --- | --- |\n"
                   + "".join(f"| 区域{i} | 规则{i} |\n" for i in range(40)))
        block = ParsedBlock(block_id="t1", block_type="table_markdown", source_name="a.pdf", content=content)
        chunks = chunk_table_block(block, 200)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertIn("| 配送区域 | 运费规则 |", chunk.content)
            self.assertIn("| ---", chunk.content)

    def test_parent_builder_links_children_and_generates_summary(self):
        blocks = [ParsedBlock(block_id=f"b{i}", block_type="text", source_name="a.pdf",
                              content=f"{i} 内容" * 50) for i in range(1, 5)]
        children = []
        for block in blocks:
            children.extend(chunk_text_block(block, 100, 20))
        parents = build_parent_chunks(children, max_children=6)
        self.assertGreaterEqual(len(parents), 1)
        self.assertEqual(parents[0].representation_level, "coarse")
        self.assertEqual(parents[0].chunk_type, "parent_summary")
        self.assertTrue(all(child.parent_chunk_key is not None for child in children))
        self.assertEqual(parents[0].chunk_metadata["summary_method"], "fallback_truncate")


class ParserTests(unittest.TestCase):
    def test_parse_pdf_extracts_text_and_table_blocks(self):
        source = DATA_DIR / "店铺服务_发货时效与配送说明.pdf"
        parsed = parse_pdf(document_key="doc-x", source_name=source.name, file_path=source)
        self.assertEqual(parsed.source_type, "pdf")
        self.assertTrue(parsed.blocks, "should extract blocks")
        text = "\n".join(block.content for block in parsed.blocks)
        self.assertIn("发货时效", text)
        table_blocks = [block for block in parsed.blocks if block.block_type == "table_markdown"]
        self.assertTrue(table_blocks, "should extract markdown tables")
        self.assertTrue(any("运费规则" in block.content for block in table_blocks))

    def test_parse_pdf_extracts_product_keywords(self):
        source = DATA_DIR / "商品说明_轻享无线耳机P100.pdf"
        parsed = parse_pdf(document_key="doc-x", source_name=source.name, file_path=source)
        self.assertIn("P100", parsed.keywords)


class EmbeddingAndBM25Tests(unittest.TestCase):
    def test_local_hash_embedding_dim_and_cosine(self):
        from app.commerce.rag.embeddings import LocalHashEmbedding
        embedding = LocalHashEmbedding()
        vector = embedding.embed_query("包邮规则")
        self.assertEqual(len(vector), 384)
        self.assertAlmostEqual(sum(v * v for v in vector), 1.0, places=4)

    def test_remote_embedding_fallback_preserves_vector_dimension(self):
        from types import SimpleNamespace
        from app.commerce.rag.embeddings import LocalHashEmbedding, ResilientEmbeddingClient

        class FakeEmbeddings:
            calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1] * 1024)])
                raise TimeoutError("simulated remote timeout")

        remote = SimpleNamespace(client=SimpleNamespace(embeddings=FakeEmbeddings()), model_name="fake")
        embedder = ResilientEmbeddingClient(remote=remote, local=LocalHashEmbedding(384))
        self.assertEqual(len(embedder.embed_documents(["first"])[0]), 1024)
        self.assertEqual(len(embedder.embed_documents(["fallback"])[0]), 1024)

    def test_bm25_ranks_keyword_matches(self):
        from app.commerce.rag.schemas import ChunkPayload
        chunks = [
            ChunkPayload(chunk_key="a" * 32, content="包邮规则：普通配送区域商品实付金额满 99 元包邮",
                         chunk_metadata={"keywords": []}),
            ChunkPayload(chunk_key="b" * 32, content="退货需要保持商品完好",
                         chunk_metadata={"keywords": []}),
        ]
        bm25 = BM25Lite(chunks)
        results = bm25.search("包邮多少钱", top_k=2)
        self.assertEqual(results[0].chunk_key, "a" * 32)
        self.assertEqual(results[0].backend, "bm25")


class RewriteDegradeTests(unittest.TestCase):
    def test_rewrite_without_api_key_degrades_to_original_question(self):
        from app.commerce.rag.rewrite import RewriteService
        service = RewriteService(api_key="")
        self.assertFalse(service.is_available())
        self.assertEqual(service.rewrite_question(question="运费多少", history=[]), "运费多少")
        self.assertEqual(service.generate_step_back_query(question="运费多少"), "运费多少")

    def test_summarize_history_below_threshold_only_trims(self):
        from app.commerce.rag.rewrite import RewriteService
        service = RewriteService(api_key="")
        history = [item for i in range(3) for item in (
            {"role": "user", "content": f"问题{i}"},
            {"role": "assistant", "content": f"回答{i}"},
        )]
        prepared = service.summarize_history(history, threshold_turns=6, keep_recent=3,
                                             max_recent_chars=1000, max_source_chars=4000,
                                             max_summary_tokens=160)
        self.assertEqual(prepared, history)
        prepared2 = service.summarize_history(history, threshold_turns=6, keep_recent=1,
                                              max_recent_chars=1000, max_source_chars=4000,
                                              max_summary_tokens=160)
        self.assertEqual(len(prepared2), 2)


class RAGServiceRoundTripTests(unittest.TestCase):
    """离线入库 + 检索回路（临时索引目录，本地哈希 embedding）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        from app.commerce.config import Settings
        from app.commerce.rag.service import RAGService
        from app.commerce.store import Store
        self.settings = Settings(
            api_key="",  # 离线：无远端 embedding / 无改写 LLM
            rag_data_dir=str(DATA_DIR),
            rag_qdrant_path=str(Path(self.temp.name) / "qdrant"),
            rag_database_path=str(Path(self.temp.name) / "rag.sqlite"),
        )
        store = Store("sqlite:///" + str(Path(self.temp.name) / "business.db"))
        self.rag = RAGService(store, self.settings)

    def tearDown(self):
        # Qdrant 本地模式持有 .lock 文件，Windows 下需先关闭客户端再清理临时目录
        try:
            self.rag.qdrant.client.close()
        except Exception:
            pass
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_build_and_search_roundtrip(self):
        result = self.rag.build(force=True)
        self.assertEqual(result["total"], len(list(DATA_DIR.glob("*.pdf"))))
        self.assertEqual(len(result["changed"]), result["total"])
        self.assertTrue(self.rag.ready())

        response = self.rag.search(query="包邮规则是什么")
        self.assertEqual(response["status"], "ok")
        self.assertTrue(response["hits"])
        self.assertEqual(response["hits"][0]["version"], "rag-demo-v1")
        self.assertIn("plan", response)
        self.assertIn("route", response["plan"])

        # 商品编号过滤：P100 问题应优先命中 P100 文档
        filtered = self.rag.search(query="续航多久", product_id="P100")
        self.assertTrue(any("P100" in hit["title"] for hit in filtered["hits"]))

    def test_search_when_not_ready_raises_rag_not_ready(self):
        from app.commerce.rag.service import RagNotReady
        # 未构建任何索引：直接查询应抛 RagNotReady（调用方回退关键词检索）
        with self.assertRaises(RagNotReady):
            self.rag.search(query="包邮")


class ToolSchemaTests(unittest.TestCase):
    def test_rag_search_registered_as_read_tool(self):
        from app.commerce.tools import RAGSearchArgs, READ_TOOLS, SCHEMAS
        self.assertIn("rag_search", SCHEMAS)
        self.assertIn("rag_search", READ_TOOLS)
        args = SCHEMAS["rag_search"].model_validate({"query": "运费", "product_id": "P100"})
        self.assertEqual(args.product_id, "P100")

    def test_rag_search_history_is_bounded(self):
        from app.commerce.tools import RAGSearchArgs
        from pydantic import ValidationError
        history = [{"role": "user", "content": f"问题{i}"} for i in range(9)]
        with self.assertRaises(ValidationError):
            RAGSearchArgs.model_validate({"query": "运费", "history": history})


if __name__ == "__main__":
    unittest.main()
