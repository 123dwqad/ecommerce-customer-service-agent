# 电商知识库 RAG 工具（rag_search）

电商客服 Agent 的知识库支持多格式解析、多策略切块、父子块、Query 改写与 Step Back、多路召回与 RRF 融合。文档源为 `raw_data/pdf/` 下的演示文档，生成方式见 `scripts/generate_commerce_docs.py`。

## 当前实现

| 能力 | 实现 | 代码 |
|---|---|---|
| PDF 解析 | 可选 MinerU 在线 API / 本地 CLI；失败回退 pdfplumber | `app/commerce/rag/parsers.py` |
| 切块 | fixed、recursive、heading、policy_clause、procedure_step；支持表格与父子块 | `app/commerce/rag/chunkers.py` |
| 向量 | 优先远程 embedding，失败回退本地哈希向量 | `app/commerce/rag/embeddings.py` |
| 检索 | Qdrant + BM25/可选 Elasticsearch，RRF 融合，SQLite LIKE 兜底 | `app/commerce/rag/retrieval.py`、`stores.py` |
| 存储 | SQLite（`data/rag.sqlite`）+ 本地 Qdrant（`data/rag_qdrant`）+ 可选 ES | `app/commerce/rag/ingest.py` |
| 重排 | 保留可选扩展位，默认未启用 | - |

## 检索链路

```text
当前问题 + 最近几轮脱敏对话
  → 历史压缩（≤6 轮只截断不调模型；超长取头 1/3 + 尾 2/3）
  → Query 改写（结合历史补全商品编号、规则条件；无模型 Key 时原样返回）
  → Step Back 回溯问题
  → 意图路由：keyword（编号/表格/价格）/ vector（怎么/如何/流程）/ hybrid
  → 多路并行召回：BM25 或 ES + Qdrant（原问题与回溯问题各一路）
  → RRF 融合 → Top-K
  → 父块摘要补充（child 精确命中 + parent 页面/章节背景）
```

入库链路：解析 PDF → 正文/表格 block → 策略自动选择切分（政策说明按条款、流程按步骤、表格保留表头按行）→ 父子块（父摘要 LLM 可用时调用，否则截断兜底）→ SQLite + Qdrant + 可选 ES 三写。

## 配置（均带默认值，离线可用）

```dotenv
COMMERCE_RAG_ENABLED=true            # 关闭后回退 search_knowledge 关键词检索
COMMERCE_RAG_DATA_DIR=raw_data/pdf
COMMERCE_RAG_PARSER=mineru_api      # 优先 MinerU 在线 API；失败/无 Token 时回退 pdfplumber
COMMERCE_RAG_MINERU_TIMEOUT_SECONDS=300
COMMERCE_RAG_MINERU_API_URL=https://mineru.net/api/v4
COMMERCE_RAG_MINERU_API_KEY=         # MinerU 控制台创建的 API Token
COMMERCE_RAG_MINERU_MODEL=vlm        # vlm / pipeline
COMMERCE_RAG_DATABASE_PATH=./data/rag.sqlite
COMMERCE_RAG_QDRANT_PATH=./data/rag_qdrant
COMMERCE_RAG_COLLECTION_NAME=commerce_rag_chunks
COMMERCE_RAG_ES_URL=                 # 空 = 不启用 ES，走 BM25 + SQLite LIKE 兜底
COMMERCE_RAG_ES_INDEX=commerce_rag_chunks
COMMERCE_RAG_ES_TIMEOUT_SECONDS=1
COMMERCE_RAG_ES_CIRCUIT_SECONDS=60   # ES 失败后熔断时长
COMMERCE_RAG_TOP_K=5
COMMERCE_RAG_CANDIDATE_TOP_K=12
COMMERCE_RAG_PARALLEL_WORKERS=4
COMMERCE_RAG_CHUNK_SIZE=800
COMMERCE_RAG_CHUNK_OVERLAP=100
COMMERCE_RAG_PARENT_ENABLED=true
COMMERCE_RAG_MODEL=qwen-plus         # Query 改写 / 历史压缩 / 父摘要模型
COMMERCE_RAG_LLM_TIMEOUT_SECONDS=4
COMMERCE_RAG_EMBEDDING_PROVIDER=auto # auto/remote/local；remote 失败自动降级本地哈希向量
COMMERCE_RAG_EMBEDDING_MODEL=text-embedding-v4
```

- 未配置 `DASHSCOPE_API_KEY` 时：改写链路原样返回（不调模型），embedding 用本地 384 维哈希向量，父摘要走截断兜底——整条链路离线可用。
- 有 Key 时：改写、Step Back、父摘要走 qwen；embedding 走百炼 `text-embedding-v4`（1024 维，单批 ≤10 自动分批）。远端失败一次后自动降级本地并告警一次。
- Qdrant 集合维度按首个向量自适应创建；维度不匹配且集合非空时跳过写入保护已有数据，SQLite 与关键词检索不受影响。

## 构建

默认优先使用 MinerU 在线 API：配置有效 Token 后，原有构建命令会将 PDF 上传解析，再复用项目切块与索引流程；在线 API 失败、超时、返回空结果或未配置 Token 时，该文件自动回退到原有 pdfplumber 解析。若设置 `COMMERCE_RAG_PARSER=mineru` 则使用本机 MinerU CLI；设置为 `pdfplumber` 可强制使用原解析方式。MinerU 在线服务有文件大小、页数和账号额度限制，文档会上传至第三方服务处理。详见 [MinerU 官方 API 文档](https://mineru.net/apiManage/docs)。

```powershell
.\.venv\Scripts\python.exe -m scripts.build_commerce_rag            # 增量（按文件 mtime）
.\.venv\Scripts\python.exe -m scripts.build_commerce_rag --rebuild  # 全量
.\.venv\Scripts\python.exe -m scripts.build_commerce_rag --ask "P100 耳机续航多久"  # 冒烟检索
```

首次使用且索引未就绪时，`rag_search` 会在后台线程触发构建，当前请求回退 `search_knowledge`（不会阻塞超时）；构建完成后自动切换。

## 在 Agent 中的接入

- 新工具 `rag_search`（只读，本地与 MCP 两种 transport 均可用），Schema 在 `app/commerce/tools.py`，分发在 `app/commerce/service.py`。
- `product_consult` / `other` 意图优先走 `rag_search`（传入最近 8 条脱敏消息做历史压缩），`rag_unavailable` / 超时时回退 `search_knowledge`。
- 可选 LLM 回答模式（ReAct）下，检索工具同样切换为 `rag_search`，每个工具调用仍限 2 次。
- 售后资格判定**不经过 RAG**：仍由 `check_after_sales` + 订单绑定政策版本精确判定，RAG 只负责解释流程与政策文本。

## 边界

- 文档与索引都是演示数据；回答只基于检索到的 chunk，工具输出带 `source`/`version`/`locator` 供溯源。
- 改文档后需重新构建（mtime 变化自动增量重建对应文档）。
- 测试：`python -m unittest tests.test_rag -v`（14 例，离线运行）。
