# -*- coding: utf-8 -*-
"""Embedding 客户端：远端（OpenAI 兼容接口）失败时自动降级为本地哈希向量。

- LocalHashEmbedding：384 维哈希向量，无任何外部依赖，离线可用；
- ResilientEmbeddingClient：远端可用用远端，远端失败一次后切本地并告警一次。
"""
from __future__ import annotations

import hashlib
import logging
import re
from functools import lru_cache

import numpy as np

logger = logging.getLogger(__name__)


class LocalHashEmbedding:
    def __init__(self, dim: int = 384):
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_text(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_text(text)

    def _embed_text(self, text: str) -> list[float]:
        vector = np.zeros(self.dim, dtype=np.float32)
        tokens = self._tokenize(text)
        if not tokens:
            return vector.tolist()
        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], byteorder="big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector /= norm
        return vector.tolist()

    def _tokenize(self, text: str) -> list[str]:
        normalized = " ".join(text.lower().split())
        word_tokens = re.findall(r"[a-z0-9_]+", normalized)
        cjk_chars = re.findall(r"[一-鿿]", normalized)
        cjk_bigrams = [normalized[index:index + 2] for index in range(max(len(normalized) - 1, 0))
                       if re.match(r"[一-鿿]{2}", normalized[index:index + 2])]
        return word_tokens + cjk_chars + cjk_bigrams


class ResilientEmbeddingClient:
    def __init__(self, remote=None, local: LocalHashEmbedding | None = None):
        self.remote = remote
        self.local = local or LocalHashEmbedding()
        self._warned_remote_failure = False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.remote is not None:
            try:
                # 百炼 text-embedding 接口单批最多 10 条，分批请求后按序拼接。
                vectors: list = []
                for batch in _batches(texts, 10):
                    response = self.remote.client.embeddings.create(model=self.remote.model_name, input=batch)
                    vectors.extend(item.embedding for item in response.data)
                if vectors:
                    # Keep the offline fallback in the same vector space as the
                    # remote model. Otherwise one transient API failure can make
                    # subsequent Qdrant writes/queries use a different dimension.
                    self.local.dim = len(vectors[0])
                return vectors
            except Exception as exc:
                self._disable_remote(exc)
        return self.local.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        if self.remote is not None:
            try:
                vectors = self.remote.client.embeddings.create(model=self.remote.model_name, input=[text])
                vector = vectors.data[0].embedding
                self.local.dim = len(vector)
                return vector
            except Exception as exc:
                self._disable_remote(exc)
        return self.local.embed_query(text)

    def _disable_remote(self, exc: Exception) -> None:
        if not self._warned_remote_failure:
            logger.warning("Remote embedding unavailable, falling back to local hash embedding: %s", exc)
            self._warned_remote_failure = True
        self.remote = None


class _RemoteEmbedding:
    """OpenAI 兼容 embeddings 接口的轻封装。"""

    def __init__(self, client, model_name: str):
        self.client = client
        self.model_name = model_name


def _batches(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]


@lru_cache
def get_embedding_client(provider: str, api_key: str, base_url: str, model: str) -> ResilientEmbeddingClient:
    remote = None
    if provider in {"auto", "remote"} and api_key:
        try:
            from openai import OpenAI
            remote = _RemoteEmbedding(
                OpenAI(api_key=api_key, base_url=base_url, timeout=10, max_retries=0),
                model,
            )
        except Exception as exc:
            logger.warning("Remote embedding client unavailable: %s", exc)
    return ResilientEmbeddingClient(remote=remote)
