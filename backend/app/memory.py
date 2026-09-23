"""Short-term conversation memory in Redis, keyed by conversation ID.

Layout per conversation (all keys expire after `conversation_ttl_seconds`):
  conv:{id}:messages  -> Redis list of JSON {"role", "content"} (trimmed)
  conv:{id}:context   -> JSON blob: active company + last workflow outputs

A generic `cache:*` namespace is also exposed so the SEC layer can cache
filing summaries by accession number (filings never change once filed).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis

from app.config import get_settings

log = logging.getLogger(__name__)


class _InMemoryRedis:
    """Tiny subset of the redis.asyncio API used below; dev fallback only."""

    def __init__(self) -> None:
        self._kv: dict[str, Any] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._kv[key] = value

    async def rpush(self, key: str, *values: str) -> None:
        self._kv.setdefault(key, []).extend(values)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        items = self._kv.get(key, [])
        n = len(items)
        s = start if start >= 0 else max(n + start, 0)
        e = end if end >= 0 else n + end
        self._kv[key] = items[s : e + 1]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self._kv.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self._kv.pop(k, None)


class MemoryStore:
    def __init__(self, client: Any) -> None:
        self.r = client
        s = get_settings()
        self.ttl = s.conversation_ttl_seconds
        self.max_messages = s.max_history_messages

    # ---- conversation history -------------------------------------------
    async def get_history(self, conv_id: str) -> list[dict[str, str]]:
        raw = await self.r.lrange(f"conv:{conv_id}:messages", 0, -1)
        return [json.loads(m) for m in raw]

    async def append_messages(self, conv_id: str, *messages: dict[str, str]) -> None:
        key = f"conv:{conv_id}:messages"
        await self.r.rpush(key, *(json.dumps(m) for m in messages))
        await self.r.ltrim(key, -self.max_messages, -1)
        await self.r.expire(key, self.ttl)

    # ---- conversation context (active company, last results) -------------
    async def get_context(self, conv_id: str) -> dict[str, Any]:
        raw = await self.r.get(f"conv:{conv_id}:context")
        return json.loads(raw) if raw else {}

    async def set_context(self, conv_id: str, ctx: dict[str, Any]) -> None:
        await self.r.set(f"conv:{conv_id}:context", json.dumps(ctx, default=str), ex=self.ttl)

    async def clear(self, conv_id: str) -> None:
        await self.r.delete(f"conv:{conv_id}:messages", f"conv:{conv_id}:context")

    # ---- generic cache ----------------------------------------------------
    async def cache_get(self, key: str) -> Any | None:
        raw = await self.r.get(f"cache:{key}")
        return json.loads(raw) if raw else None

    async def cache_set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self.r.set(f"cache:{key}", json.dumps(value, default=str), ex=ttl)


_store: MemoryStore | None = None


async def get_store() -> MemoryStore:
    global _store
    if _store is None:
        url = get_settings().redis_url
        client: Any = redis.from_url(url, decode_responses=True)
        try:
            await client.ping()
            log.info("Connected to Redis at %s", url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis unavailable (%s); using in-memory store", exc)
            client = _InMemoryRedis()
        _store = MemoryStore(client)
    return _store


def set_store(store: MemoryStore | None) -> None:
    """Test hook."""
    global _store
    _store = store
