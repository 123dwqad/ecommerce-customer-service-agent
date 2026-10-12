import asyncio
import json
import time
from contextlib import asynccontextmanager

from app.commerce.service import BusinessError


class ConversationMemory:
    """Isolate sessions and conversations; guest identifiers are session-scoped."""
    def __init__(self, redis_url):
        self.redis = None
        self.local = {}
        self.locks = {}
        if redis_url:
            import redis.asyncio as redis
            self.redis = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)

    def key(self, actor, conversation_id):
        return f"commerce:conversation:v1:{actor[1]}:{conversation_id}"

    @asynccontextmanager
    async def locked(self, key):
        if self.redis:
            try:
                lock = self.redis.lock(key + ":lock", timeout=90, blocking_timeout=0)
                if not await lock.acquire():
                    raise BusinessError("conversation_busy", "该会话正在处理上一条消息，请稍后再试。")
            except BusinessError:
                raise
            except Exception as exc:
                raise BusinessError("memory_unavailable", "会话服务暂时不可用，请稍后再试。") from exc
            try:
                yield
            finally:
                await lock.release()
        else:
            # Local mode supports a single API process. Redis supplies distributed locks.
            lock = self.locks.setdefault(key, asyncio.Lock())
            if lock.locked():
                raise BusinessError("conversation_busy", "该会话正在处理上一条消息，请稍后再试。")
            async with lock:
                yield
            self.locks.pop(key, None)

    async def get(self, key):
        if self.redis:
            value = await self.redis.get(key)
            return json.loads(value) if value else {}
        for expired in [k for k, (expires, _) in self.local.items() if expires <= time.time()]:
            self.local.pop(expired, None)
        if len(self.local) > 10000:
            self.local.pop(next(iter(self.local)))
        return self.local.get(key, (0, {}))[1].copy()

    async def save(self, key, state):
        if self.redis:
            await self.redis.set(key, json.dumps(state, ensure_ascii=False), ex=1800)
        else:
            self.local[key] = (time.time() + 1800, state)

    async def close(self):
        if self.redis:
            await self.redis.aclose()
