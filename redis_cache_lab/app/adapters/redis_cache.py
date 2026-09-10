"""redis.asyncio adapter. The only module that knows Redis exists."""

from __future__ import annotations

import redis.asyncio as aioredis

# Monotonic pointer advance. A script because GET-then-SET from the client has
# a gap between the two round trips, and the gap is the whole bug (scenario 03).
# Redis runs a script to completion with nothing interleaved.
_ADVANCE = """
local current = redis.call('GET', KEYS[1])
if current and tonumber(current) >= tonumber(ARGV[1]) then
  return 0
end
if ARGV[2] == '0' then
  redis.call('SET', KEYS[1], ARGV[1])
else
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
end
return 1
"""


class RedisCacheGateway:
    def __init__(self, url: str):
        self.client = aioredis.from_url(url, decode_responses=True)
        self._advance = self.client.register_script(_ADVANCE)

    async def get(self, key: str) -> str | None:
        return await self.client.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None) -> None:
        # A TTL is the default, and its absence has to be explicit. Scenario 02
        # is the story of a key that quietly had none.
        await self.client.set(key, value, ex=ttl_seconds)

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return await self.client.delete(*keys)

    async def incr(self, key: str) -> int:
        return await self.client.incr(key)

    async def advance_pointer(self, key: str, version: int, ttl_seconds: int | None) -> bool:
        return bool(await self._advance(keys=[key], args=[version, ttl_seconds or 0]))

    # -- lab affordances, not part of the port ------------------------------

    async def dump(self) -> list[dict]:
        """SCAN, never KEYS — even here, where it could not matter. Habits are
        the point; see scenario 07 for what KEYS does to a real keyspace."""
        out = []
        async for key in self.client.scan_iter("*", count=100):
            out.append(
                {"key": key, "ttl": await self.client.ttl(key), "value": await self.client.get(key)}
            )
        return sorted(out, key=lambda k: k["key"])

    async def stats(self) -> dict:
        info = await self.client.info("stats")
        hits, misses = info["keyspace_hits"], info["keyspace_misses"]
        total = hits + misses
        return {
            "keyspace_hits": hits,
            "keyspace_misses": misses,
            "hit_rate": round(hits / total, 4) if total else None,
            "evicted_keys": info["evicted_keys"],
            "expired_keys": info["expired_keys"],
            "dbsize": await self.client.dbsize(),
        }

    async def flush(self) -> None:
        await self.client.flushdb()

    async def close(self) -> None:
        await self.client.aclose()
