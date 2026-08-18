import uuid
from unittest.mock import AsyncMock, MagicMock

from app import outbox_relay


async def test_relay_once_removes_spool_file_only_on_successful_publish(monkeypatch):
    request_id_a = uuid.uuid4()
    request_id_b = uuid.uuid4()

    monkeypatch.setattr(
        outbox_relay.outbox,
        "spool_read_all",
        lambda: [
            (request_id_a, {"student_id": 1, "course_id": 1}),
            (request_id_b, {"student_id": 2, "course_id": 1}),
        ],
    )
    monkeypatch.setattr(
        outbox_relay.mq, "publish_request", AsyncMock(side_effect=lambda rid: rid == request_id_a)
    )
    spool_remove_mock = MagicMock()
    monkeypatch.setattr(outbox_relay.outbox, "spool_remove", spool_remove_mock)

    relayed_count = await outbox_relay.relay_once()

    assert relayed_count == 1
    spool_remove_mock.assert_called_once_with(request_id_a)


async def test_relay_once_returns_zero_when_spool_is_empty(monkeypatch):
    monkeypatch.setattr(outbox_relay.outbox, "spool_read_all", lambda: [])

    relayed_count = await outbox_relay.relay_once()

    assert relayed_count == 0
