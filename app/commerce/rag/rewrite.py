# -*- coding: utf-8 -*-
"""Query 增强：历史压缩 → 问题改写 → Step Back 回溯（电商口径）。

- 历史 ≤ 阈值轮次只截断，不调用摘要模型；
- 超长历史取头 1/3 + 尾 2/3 交给摘要模型；
- 无模型 Key 时全部优雅降级为原问题，检索链路照常运行。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RewriteService:
    def __init__(self, *, api_key: str = "", base_url: str = "", model: str = "qwen-plus",
                 timeout: float = 8.0, max_tokens: int = 200):
        self.model = model
        self.max_tokens = max_tokens
        self.client = None
        if api_key:
            try:
                import httpx
                from openai import OpenAI
                self.client = OpenAI(
                    api_key=api_key,
                    base_url=base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    timeout=timeout,
                    max_retries=0,
                    http_client=httpx.Client(trust_env=False, timeout=timeout),
                )
            except Exception as exc:
                logger.warning("Rewrite LLM unavailable: %s", exc)

    def is_available(self) -> bool:
        return self.client is not None

    def _complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
        if not self.client:
            raise RuntimeError("LLM unavailable")
        request = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        response = self.client.chat.completions.create(**request)
        return (response.choices[0].message.content or "").strip()

    def summarize_history(
        self,
        history: list[dict],
        *,
        threshold_turns: int,
        keep_recent: int,
        max_recent_chars: int,
        max_source_chars: int,
        max_summary_tokens: int,
    ) -> list[dict]:
        turns, prefix = self._split_history_turns(history)
        complete_turns = [turn for turn in turns if any(item.get("role") == "assistant" for item in turn)]
        if not self.client or len(complete_turns) <= threshold_turns:
            return self.trim_history(history, keep_recent=keep_recent, max_chars=max_recent_chars)
        older_turns = complete_turns[:-keep_recent] if keep_recent > 0 else complete_turns
        recent_turns = complete_turns[-keep_recent:] if keep_recent > 0 else []
        incomplete_turns = [turn for turn in turns if not any(item.get("role") == "assistant" for item in turn)]
        older_messages = [item for turn in older_turns for item in turn]
        older_text = "\n".join(f"{item['role']}: {item['content']}" for item in older_messages)
        prompt = (
            "请将以下电商客服对话历史压缩成一段简洁中文摘要，"
            "保留商品、订单、售后规则、数量、时间、金额条件和未解决问题。"
            "只输出摘要，不要加前缀或解释。\n\n"
            f"{self._bound_text(older_text, max_source_chars)}"
        )
        try:
            summary = self._complete(prompt, max_tokens=max_summary_tokens)
        except Exception:
            summary = ""
        recent_messages = [item for turn in recent_turns + incomplete_turns for item in turn]
        recent_messages = self.trim_history(recent_messages, keep_recent=keep_recent, max_chars=max_recent_chars)
        parts = prefix[:]
        if summary:
            parts.append({"role": "assistant", "content": f"[history_summary] {summary}"})
        return parts + recent_messages

    @staticmethod
    def trim_history(history: list[dict], *, keep_recent: int, max_chars: int) -> list[dict]:
        turns, prefix = RewriteService._split_history_turns(history)
        selected_turns: list = []
        used_chars = sum(len(str(item.get("content", ""))) for item in prefix)
        for turn in reversed(turns):
            if len(selected_turns) >= keep_recent:
                break
            turn_chars = sum(len(str(item.get("content", ""))) for item in turn)
            if used_chars + turn_chars > max_chars:
                break
            selected_turns.append(turn)
            used_chars += turn_chars
        selected_turns.reverse()
        return prefix + [item for turn in selected_turns for item in turn]

    @staticmethod
    def _bound_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        head_chars = max_chars // 3
        tail_chars = max_chars - head_chars
        return f"{text[:head_chars]}\n[older history omitted]\n{text[-tail_chars:]}"

    @staticmethod
    def _split_history_turns(history: list[dict]) -> tuple:
        turns: list = []
        prefix: list = []
        current: list | None = None
        for item in history:
            if item.get("role") == "user":
                if current is not None:
                    turns.append(current)
                current = [item]
            elif current is None:
                prefix.append(item)
            else:
                current.append(item)
        if current is not None:
            turns.append(current)
        return turns, prefix

    def rewrite_question(self, *, question: str, history: list[dict]) -> str:
        """结合历史把口语化/省略问题改写成可独立检索的电商知识库查询。"""
        if not self.client or not history:
            return question
        history_text = "\n".join(f"{item['role']}: {item['content']}" for item in history)
        prompt = (
            "请将用户当前问题改写成一个可独立检索的电商知识库查询。\n"
            "保留商品编号、品类、订单、售后规则、金额、数量、配送区域、时间和流程等具体细节。\n"
            "只输出改写后的查询，不要解释。\n\n"
            f"历史对话：\n{history_text}\n\n"
            f"当前问题：\n{question}"
        )
        try:
            rewritten = self._complete(prompt, max_tokens=self.max_tokens)
            return rewritten or question
        except Exception:
            return question

    def generate_step_back_query(self, *, question: str) -> str:
        """将过窄问题抽象为上位问题，提升召回覆盖率。"""
        if not self.client:
            return question
        prompt = (
            "你是一位电商客服知识检索专家。给定一个具体的商品或售后问题，请生成一个更抽象、更通用的回溯问题。\n"
            "这个问题用于检索该问题依赖的背景知识和基本原则。\n"
            "只输出回溯问题，不要附加解释。\n\n"
            f"具体问题：\n{question}"
        )
        try:
            rewritten = self._complete(prompt, max_tokens=self.max_tokens)
            return rewritten or question
        except Exception:
            return question
