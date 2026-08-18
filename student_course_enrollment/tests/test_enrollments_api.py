import uuid
from unittest.mock import AsyncMock, MagicMock

from app.models import RequestStatus


def test_enroll_returns_202_and_spools_when_publish_fails(client, monkeypatch):
    fake_request_id = uuid.uuid4()
    fake_request = MagicMock(id=fake_request_id)

    monkeypatch.setattr("app.api.enrollments.db.create_enrollment_request", AsyncMock(return_value=fake_request))
    monkeypatch.setattr("app.api.enrollments.mq.publish_request", AsyncMock(return_value=False))
    spool_write_mock = MagicMock()
    monkeypatch.setattr("app.api.enrollments.outbox.spool_write", spool_write_mock)

    response = client.post("/courses/1/enroll", json={"student_id": 42})

    assert response.status_code == 202
    assert response.json() == {"request_id": str(fake_request_id)}
    spool_write_mock.assert_called_once_with(fake_request_id, {"student_id": 42, "course_id": 1})


def test_enroll_does_not_spool_when_publish_succeeds(client, monkeypatch):
    fake_request_id = uuid.uuid4()
    fake_request = MagicMock(id=fake_request_id)

    monkeypatch.setattr("app.api.enrollments.db.create_enrollment_request", AsyncMock(return_value=fake_request))
    monkeypatch.setattr("app.api.enrollments.mq.publish_request", AsyncMock(return_value=True))
    spool_write_mock = MagicMock()
    monkeypatch.setattr("app.api.enrollments.outbox.spool_write", spool_write_mock)

    response = client.post("/courses/1/enroll", json={"student_id": 42})

    assert response.status_code == 202
    spool_write_mock.assert_not_called()


def test_get_status_returns_404_when_missing(client, monkeypatch):
    monkeypatch.setattr("app.api.enrollments.db.get_enrollment_request", AsyncMock(return_value=None))

    response = client.get(f"/enrollments/requests/{uuid.uuid4()}")

    assert response.status_code == 404


def test_get_status_returns_status(client, monkeypatch):
    request_id = uuid.uuid4()
    fake_request = MagicMock(id=request_id, student_id=1, course_id=2, status=RequestStatus.PENDING)
    monkeypatch.setattr("app.api.enrollments.db.get_enrollment_request", AsyncMock(return_value=fake_request))

    response = client.get(f"/enrollments/requests/{request_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
