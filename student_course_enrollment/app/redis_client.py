import uuid
from typing import AsyncIterator


async def cache_status(request_id: uuid.UUID, status: str, ttl_seconds: int) -> None:
    """Write the current status to a Redis key (e.g. f"status:{request_id}") with a TTL.

    Lets a late-connecting SSE client see a status that already fired
    before it subscribed to the Pub/Sub channel.
    """
    raise NotImplementedError


async def get_cached_status(request_id: uuid.UUID) -> str | None:
    """Read the cached status key written by cache_status, or None if absent/expired."""
    raise NotImplementedError


async def publish_status(request_id: uuid.UUID, status: str) -> None:
    """Publish the new status to a Redis Pub/Sub channel (e.g. f"enrollment_status:{request_id}")."""
    raise NotImplementedError


async def subscribe_status(request_id: uuid.UUID) -> AsyncIterator[str]:
    """Subscribe to the Pub/Sub channel for this request_id and yield each status as it arrives.

    Should keep yielding until the caller stops iterating (e.g. breaks out
    of the loop after a terminal status) - does not need to know about
    terminal statuses itself.
    """
    raise NotImplementedError
    yield  # pragma: no cover - makes this an async generator
