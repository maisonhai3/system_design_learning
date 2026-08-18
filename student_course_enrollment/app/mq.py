import uuid
from typing import Awaitable, Callable


async def publish_request(request_id: uuid.UUID) -> bool:
    """Publish {request_id} to the enrollment_requests queue.

    Returns True on success, False on failure (e.g. broker unreachable) -
    must not raise, so callers can fall back to the local disk spool.
    """
    raise NotImplementedError


async def consume_requests(
    callback: Callable[[uuid.UUID, Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]], Awaitable[None]],
) -> None:
    """Long-running loop over the enrollment_requests queue.

    For each message, parse the request_id and call
    `await callback(request_id, ack, nack)`, where `ack`/`nack` are no-arg
    async callables that acknowledge or reject-and-requeue the message.
    Does not return until cancelled.
    """
    raise NotImplementedError
