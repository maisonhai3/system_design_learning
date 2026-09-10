"""Inspection endpoints. A lab affordance; do not ship these."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.adapters.redis_cache import RedisCacheGateway
from app.api import deps

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/keys")
async def keyspace(cache: Annotated[RedisCacheGateway, Depends(deps.get_cache)]):
    """Every key with its TTL. Read `ttl: -1` as 'no expiry' — scenario 02."""
    return await cache.dump()


@router.get("/stats")
async def stats(cache: Annotated[RedisCacheGateway, Depends(deps.get_cache)]):
    """hit_rate is the number people quote. evicted_keys is the one that bites."""
    return await cache.stats()


@router.post("/flush")
async def flush(cache: Annotated[RedisCacheGateway, Depends(deps.get_cache)]):
    await cache.flush()
    return {"flushed": True}
