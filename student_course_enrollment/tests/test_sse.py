import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from app import db, redis_client
from app.api import sse
from app.models import RequestStatus


async def test_stream_events_yields_cached_status_and_stops_on_terminal(monkeypatch):
    request_id = uuid.uuid4()

    async def fake_get_cached_status(rid):
        return RequestStatus.COMPLETED.value

    monkeypatch.setattr(redis_client, "get_cached_status", fake_get_cached_status)

    results = [status async for status in sse.stream_events(request_id, session=AsyncMock())]

    assert results == [RequestStatus.COMPLETED.value]


async def test_stream_events_subscribes_after_no_cached_status(monkeypatch):
    request_id = uuid.uuid4()

    async def fake_get_cached_status(rid):
        return None

    async def fake_subscribe_status(rid):
        yield RequestStatus.PROCESSING.value
        yield RequestStatus.COMPLETED.value

    monkeypatch.setattr(redis_client, "get_cached_status", fake_get_cached_status)
    monkeypatch.setattr(redis_client, "subscribe_status", fake_subscribe_status)

    results = [status async for status in sse.stream_events(request_id, session=AsyncMock())]

    assert results == [RequestStatus.PROCESSING.value, RequestStatus.COMPLETED.value]


async def test_stream_events_falls_back_to_polling_when_redis_times_out(monkeypatch):
    request_id = uuid.uuid4()

    async def fake_get_cached_status(rid):
        return None

    async def fake_subscribe_status(rid):
        raise asyncio.TimeoutError
        yield  # pragma: no cover - unreachable, keeps this an async generator

    poll_results = [
        MagicMock(status=MagicMock(value=RequestStatus.PROCESSING.value)),
        MagicMock(status=MagicMock(value=RequestStatus.COMPLETED.value)),
    ]

    async def fake_get_enrollment_request(session, rid):
        return poll_results.pop(0) if poll_results else None

    monkeypatch.setattr(redis_client, "get_cached_status", fake_get_cached_status)
    monkeypatch.setattr(redis_client, "subscribe_status", fake_subscribe_status)
    monkeypatch.setattr(db, "get_enrollment_request", fake_get_enrollment_request)
    monkeypatch.setattr(sse.settings, "poll_interval_seconds", 0)

    results = [status async for status in sse.stream_events(request_id, session=AsyncMock())]

    assert results == [RequestStatus.PROCESSING.value, RequestStatus.COMPLETED.value]
