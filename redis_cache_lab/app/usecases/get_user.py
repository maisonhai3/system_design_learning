"""Read path: cache-aside, in the four flavours the scenarios put under test."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from app.domain.entities import User
from app.domain.ports import CacheGateway, UserRepository
from app.usecases import keys


@dataclass
class ReadResult:
    user: User
    cache_hit: bool
    key: str


class GetUser:
    """Depends on two Protocols and nothing else.

    No FastAPI import, no SQLAlchemy import, no redis import. That is not
    aesthetic: it is what lets the same code run from a cronjob, a Kafka
    consumer or a test with two in-memory fakes.
    """

    def __init__(self, users: UserRepository, cache: CacheGateway):
        self.users = users
        self.cache = cache

    async def __call__(
        self,
        user_id: int,
        *,
        versioned: bool = False,
        ttl: int | None = keys.DEFAULT_TTL,
        stall_ms: int = 0,
    ) -> ReadResult | None:
        """`stall_ms` is a chaos knob: it widens the window between reading the
        database and writing the cache, so you can hit the scenario 03 race by
        hand with two curls instead of a thread barrier. Gated to the chaos
        routes; it is a lab, and this is the honest way to have a seam."""
        if versioned:
            return await self._versioned(user_id, ttl, stall_ms)
        return await self._plain(user_id, ttl, stall_ms)

    async def _plain(self, user_id: int, ttl: int | None, stall_ms: int):
        key = keys.user_profile(user_id)
        cached = await self.cache.get(key)
        if cached is not None:
            return ReadResult(_decode(cached), cache_hit=True, key=key)

        user = await self.users.get(user_id)
        if user is None:
            return None
        if stall_ms:
            await asyncio.sleep(stall_ms / 1000)  # ← the window
        await self.cache.set(key, _encode(user), ttl)
        return ReadResult(user, cache_hit=False, key=key)

    async def _versioned(self, user_id: int, ttl: int | None, stall_ms: int):
        pointer = keys.user_profile(user_id)
        current = await self.cache.get(pointer)
        if current is not None:
            cached = await self.cache.get(keys.user_profile_at(user_id, int(current)))
            if cached is not None:
                return ReadResult(_decode(cached), cache_hit=True, key=pointer)

        user = await self.users.get(user_id)
        if user is None:
            return None
        if stall_ms:
            await asyncio.sleep(stall_ms / 1000)

        # Written under the version it was READ at. If a writer committed while
        # we stalled, this lands on a key nobody will look up again.
        await self.cache.set(keys.user_profile_at(user_id, user.version), _encode(user), ttl)
        await self.cache.advance_pointer(pointer, user.version, ttl)
        return ReadResult(user, cache_hit=False, key=pointer)


def _encode(user: User) -> str:
    return json.dumps(user.__dict__)


def _decode(raw: str) -> User:
    return User(**json.loads(raw))
