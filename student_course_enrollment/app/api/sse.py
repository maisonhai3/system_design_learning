import asyncio
import uuid
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app import db, redis_client
from app.config import settings
from app.models import RequestStatus

TERMINAL_STATUSES = {RequestStatus.COMPLETED.value, RequestStatus.REJECTED_FULL.value}


class RedisUnavailable(Exception):
    pass


async def _subscribe_with_timeout(request_id: uuid.UUID) -> AsyncIterator[str]:
    subscription = redis_client.subscribe_status(request_id)
    while True:
        try:
            status = await asyncio.wait_for(subscription.__anext__(), timeout=settings.redis_timeout_seconds)
        except StopAsyncIteration:
            return
        except (asyncio.TimeoutError, ConnectionError) as exc:
            raise RedisUnavailable from exc
        yield status


async def _poll_fallback(request_id: uuid.UUID, session: AsyncSession) -> AsyncIterator[str]:
    while True:
        req = await db.get_enrollment_request(session, request_id)
        if req is None:
            return
        yield req.status.value
        if req.status.value in TERMINAL_STATUSES:
            return
        await asyncio.sleep(settings.poll_interval_seconds)


async def stream_events(request_id: uuid.UUID, session: AsyncSession) -> AsyncIterator[str]:
    try:
        cached = await asyncio.wait_for(
            redis_client.get_cached_status(request_id), timeout=settings.redis_timeout_seconds
        )
    except (asyncio.TimeoutError, ConnectionError):
        cached = None

    if cached is not None:
        yield cached
        if cached in TERMINAL_STATUSES:
            return

    try:
        async for status in _subscribe_with_timeout(request_id):
            yield status
            if status in TERMINAL_STATUSES:
                return
    except RedisUnavailable:
        async for status in _poll_fallback(request_id, session):
            yield status
