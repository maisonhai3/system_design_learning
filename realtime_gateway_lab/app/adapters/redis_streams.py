"""redis.asyncio adapter for Streams and Pub/Sub."""

from __future__ import annotations

from typing import AsyncIterator

import redis.asyncio as aioredis


class RedisEventLog:
    def __init__(self, url: str):
        self.client = aioredis.from_url(url, decode_responses=True)

    async def append(self, stream: str, fields: dict[str, str], maxlen: int | None) -> str:
        # approximate=True is `MAXLEN ~ n`: Redis trims to a node boundary
        # instead of walking to an exact count. Exact trimming on every XADD is
        # O(trimmed) work on the single server thread, on the hot write path.
        return await self.client.xadd(stream, fields, maxlen=maxlen, approximate=True)

    async def read(
        self, stream: str, after_id: str, block_ms: int, count: int
    ) -> list[tuple[str, dict[str, str]]]:
        """XREAD with BLOCK: the server holds the request open instead of
        answering "nothing yet" thousands of times a second."""
        result = await self.client.xread({stream: after_id}, count=count, block=block_ms)
        if not result:
            return []
        return [(entry_id, fields) for _stream, entries in result for entry_id, fields in entries]

    async def length(self, stream: str) -> int:
        return await self.client.xlen(stream)

    async def put_body(self, key: str, payload: str) -> None:
        # A TTL, because a body nobody dereferences is a leak, and the pointers
        # that name it are themselves trimmed. Outliving the pointers by a
        # margin is the whole requirement.
        await self.client.set(key, payload, ex=3600)

    async def get_body(self, key: str) -> str | None:
        return await self.client.get(key)

    # -- lab affordances ----------------------------------------------------

    async def range(self, stream: str) -> list[tuple[str, dict[str, str]]]:
        return await self.client.xrange(stream)

    async def streams(self) -> list[str]:
        out = []
        async for key in self.client.scan_iter("feed:*", count=100):
            if await self.client.type(key) == "stream":
                out.append(key)
        return sorted(out)

    async def close(self) -> None:
        await self.client.aclose()


class RedisBroadcast:
    """Pub/Sub. Here to be proven wrong for this job, in scenario 01."""

    def __init__(self, url: str):
        self.client = aioredis.from_url(url, decode_responses=True)

    async def publish(self, channel: str, message: str) -> int:
        # The return value is the number of subscribers that received it —
        # which is the closest Pub/Sub gets to a delivery receipt, and it is
        # not close: it counts CONNECTIONS at this instant, not consumers who
        # will still be alive when they try to act on it.
        return await self.client.publish(channel, message)

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        pubsub = self.client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    yield message["data"]
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def close(self) -> None:
        await self.client.aclose()
