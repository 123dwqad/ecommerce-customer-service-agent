import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env.multimodal", override=False)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("COMMERCE_DATABASE_URL", "sqlite:///./data/commerce.db"))
    redis_url: str = field(default_factory=lambda: os.getenv("COMMERCE_REDIS_URL", ""))
    tool_transport: str = field(default_factory=lambda: os.getenv("COMMERCE_TOOL_TRANSPORT", "local"))
    mcp_url: str = field(default_factory=lambda: os.getenv("COMMERCE_MCP_URL", "http://127.0.0.1:8004/sse"))
    signing_key: str = field(default_factory=lambda: os.getenv("COMMERCE_SIGNING_KEY", ""))
    llm_enabled: bool = field(default_factory=lambda: os.getenv("COMMERCE_LLM_ENABLED", "false").lower() == "true")
    api_key: str = field(default_factory=lambda: os.getenv("DASHSCOPE_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("COMMERCE_MODEL", "qwen-max"))
    intent_enabled: bool = field(default_factory=lambda: os.getenv("COMMERCE_INTENT_ENABLED", "false").lower() == "true")
    intent_model: str = field(default_factory=lambda: os.getenv("COMMERCE_INTENT_MODEL", "qwen-plus"))
    intent_base_url: str = field(default_factory=lambda: os.getenv("COMMERCE_INTENT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
    intent_api_key: str = field(default_factory=lambda: os.getenv("COMMERCE_INTENT_API_KEY", ""))
    intent_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("COMMERCE_INTENT_TIMEOUT_SECONDS", "8")))
    intent_max_retries: int = field(default_factory=lambda: int(os.getenv("COMMERCE_INTENT_MAX_RETRIES", "1")))
    intent_trust_env: bool = field(default_factory=lambda: os.getenv("COMMERCE_INTENT_TRUST_ENV", "false").lower() == "true")
    demo_enabled: bool = field(default_factory=lambda: os.getenv("COMMERCE_DEMO_ENABLED", "true").lower() == "true")
    speech_enabled: bool = field(default_factory=lambda: os.getenv("COMMERCE_SPEECH_ENABLED", "false").lower() == "true")
    speech_protocol: str = field(default_factory=lambda: os.getenv("COMMERCE_SPEECH_PROTOCOL", "multipart").strip())
    speech_url: str = field(default_factory=lambda: os.getenv("COMMERCE_SPEECH_URL", "").strip())
    speech_model: str = field(default_factory=lambda: os.getenv("COMMERCE_SPEECH_MODEL", "").strip())
    speech_api_key: str = field(default_factory=lambda: os.getenv("COMMERCE_SPEECH_API_KEY", "").strip())
    vision_enabled: bool = field(default_factory=lambda: os.getenv("COMMERCE_VISION_ENABLED", "false").lower() == "true")
    vision_url: str = field(default_factory=lambda: os.getenv("COMMERCE_VISION_URL", "").strip())
    vision_model: str = field(default_factory=lambda: os.getenv("COMMERCE_VISION_MODEL", "").strip())
    vision_api_key: str = field(default_factory=lambda: os.getenv("COMMERCE_VISION_API_KEY", "").strip())
    media_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("COMMERCE_MEDIA_TIMEOUT_SECONDS", "45")))
    # ---- RAG（电商知识库检索） ----
    rag_enabled: bool = field(default_factory=lambda: os.getenv("COMMERCE_RAG_ENABLED", "true").lower() == "true")
    rag_data_dir: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_DATA_DIR", "raw_data/pdf"))
    rag_parser: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_PARSER", "mineru_api").strip().lower())
    rag_mineru_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("COMMERCE_RAG_MINERU_TIMEOUT_SECONDS", "300")))
    rag_mineru_api_url: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_MINERU_API_URL", "https://mineru.net/api/v4").strip())
    rag_mineru_api_key: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_MINERU_API_KEY", "").strip())
    rag_mineru_model: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_MINERU_MODEL", "vlm").strip())
    rag_database_path: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_DATABASE_PATH", "./data/rag.sqlite"))
    rag_qdrant_path: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_QDRANT_PATH", "./data/rag_qdrant"))
    rag_collection_name: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_COLLECTION_NAME", "commerce_rag_chunks"))
    rag_es_url: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_ES_URL", ""))
    rag_es_index: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_ES_INDEX", "commerce_rag_chunks"))
    rag_es_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("COMMERCE_RAG_ES_TIMEOUT_SECONDS", "1")))
    rag_es_circuit_seconds: float = field(default_factory=lambda: float(os.getenv("COMMERCE_RAG_ES_CIRCUIT_SECONDS", "60")))
    rag_rewrite_enabled: bool = field(default_factory=lambda: os.getenv("COMMERCE_RAG_REWRITE_ENABLED", "auto").lower() != "false")
    rag_rewrite_model: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_MODEL", "qwen-plus"))
    rag_llm_base_url: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
    rag_llm_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("COMMERCE_RAG_LLM_TIMEOUT_SECONDS", "4")))
    rag_embedding_provider: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_EMBEDDING_PROVIDER", "auto"))
    rag_embedding_base_url: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
    rag_embedding_model: str = field(default_factory=lambda: os.getenv("COMMERCE_RAG_EMBEDDING_MODEL", "text-embedding-v4"))
    rag_top_k: int = field(default_factory=lambda: int(os.getenv("COMMERCE_RAG_TOP_K", "5")))
    rag_candidate_top_k: int = field(default_factory=lambda: int(os.getenv("COMMERCE_RAG_CANDIDATE_TOP_K", "12")))
    rag_parallel_workers: int = field(default_factory=lambda: int(os.getenv("COMMERCE_RAG_PARALLEL_WORKERS", "4")))
    rag_chunk_size: int = field(default_factory=lambda: int(os.getenv("COMMERCE_RAG_CHUNK_SIZE", "800")))
    rag_chunk_overlap: int = field(default_factory=lambda: int(os.getenv("COMMERCE_RAG_CHUNK_OVERLAP", "100")))
    rag_parent_enabled: bool = field(default_factory=lambda: os.getenv("COMMERCE_RAG_PARENT_ENABLED", "true").lower() == "true")

    def validate(self):
        if not 1 <= self.intent_timeout_seconds <= 20:
            raise ValueError("Intent timeout must be between 1 and 20 seconds")
        if not 0 <= self.intent_max_retries <= 3:
            raise ValueError("Intent max retries must be between 0 and 3")
        if self.intent_enabled and not (self.intent_api_key or self.api_key):
            raise ValueError("Intent recognition requires COMMERCE_INTENT_API_KEY or DASHSCOPE_API_KEY")
        if self.speech_protocol not in {"multipart", "dashscope_asr"}:
            raise ValueError("COMMERCE_SPEECH_PROTOCOL must be multipart or dashscope_asr")
        if not 1 <= self.media_timeout_seconds <= 90:
            raise ValueError("COMMERCE_MEDIA_TIMEOUT_SECONDS must be between 1 and 90")
        if self.tool_transport not in {"local", "mcp"}:
            raise ValueError("COMMERCE_TOOL_TRANSPORT must be local or mcp")
        if self.tool_transport == "mcp" and len(self.signing_key) < 32:
            raise ValueError("MCP requires COMMERCE_SIGNING_KEY with at least 32 characters")
        if self.llm_enabled and not self.api_key:
            raise ValueError("LLM mode requires DASHSCOPE_API_KEY")
