import uuid
from unittest.mock import AsyncMock, MagicMock

from app import worker
from app.models import RequestStatus


class _FakeSessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_session_factory(session):
    return lambda: _FakeSessionContext(session)


async def test_handle_message_acks_on_success(monkeypatch):
    request_id = uuid.uuid4()
    session = AsyncMock()
    fake_request = MagicMock(student_id=1, course_id=2)

    monkeypatch.setattr(worker.db, "get_enrollment_request", AsyncMock(return_value=fake_request))
    monkeypatch.setattr(worker.db, "try_enroll_student", AsyncMock(return_value=RequestStatus.COMPLETED))
    monkeypatch.setattr(worker.db, "mark_request_status", AsyncMock())
    publish_status_mock = AsyncMock()
    monkeypatch.setattr(worker.redis_client, "cache_status", AsyncMock())
    monkeypatch.setattr(worker.redis_client, "publish_status", publish_status_mock)

    ack = AsyncMock()
    nack = AsyncMock()

    await worker.handle_message(_fake_session_factory(session), request_id, ack, nack)

    ack.assert_called_once()
    nack.assert_not_called()
    session.commit.assert_called_once()
    publish_status_mock.assert_called_once_with(request_id, RequestStatus.COMPLETED.value)


async def test_handle_message_nacks_when_request_missing(monkeypatch):
    request_id = uuid.uuid4()
    session = AsyncMock()

    monkeypatch.setattr(worker.db, "get_enrollment_request", AsyncMock(return_value=None))

    ack = AsyncMock()
    nack = AsyncMock()

    await worker.handle_message(_fake_session_factory(session), request_id, ack, nack)

    nack.assert_called_once()
    ack.assert_not_called()


async def test_handle_message_acks_when_redis_fails_after_db_commit(monkeypatch):
    request_id = uuid.uuid4()
    session = AsyncMock()
    fake_request = MagicMock(student_id=1, course_id=2)

    monkeypatch.setattr(worker.db, "get_enrollment_request", AsyncMock(return_value=fake_request))
    monkeypatch.setattr(worker.db, "try_enroll_student", AsyncMock(return_value=RequestStatus.COMPLETED))
    monkeypatch.setattr(worker.db, "mark_request_status", AsyncMock())
    monkeypatch.setattr(worker.redis_client, "cache_status", AsyncMock())
    monkeypatch.setattr(
        worker.redis_client, "publish_status", AsyncMock(side_effect=ConnectionError("redis is down"))
    )

    ack = AsyncMock()
    nack = AsyncMock()

    await worker.handle_message(_fake_session_factory(session), request_id, ack, nack)

    session.commit.assert_called_once()
    ack.assert_called_once()
    nack.assert_not_called()


async def test_handle_message_rolls_back_and_nacks_on_error(monkeypatch):
    request_id = uuid.uuid4()
    session = AsyncMock()
    fake_request = MagicMock(student_id=1, course_id=2)

    monkeypatch.setattr(worker.db, "get_enrollment_request", AsyncMock(return_value=fake_request))
    monkeypatch.setattr(worker.db, "try_enroll_student", AsyncMock(side_effect=RuntimeError("db exploded")))

    ack = AsyncMock()
    nack = AsyncMock()

    await worker.handle_message(_fake_session_factory(session), request_id, ack, nack)

    session.rollback.assert_called_once()
    nack.assert_called_once()
    ack.assert_not_called()
