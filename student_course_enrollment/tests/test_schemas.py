import uuid

import pytest
from pydantic import ValidationError

from app.models import RequestStatus
from app.schemas import EnrollRequestIn, EnrollRequestOut, StatusOut


def test_enroll_request_in_requires_student_id():
    with pytest.raises(ValidationError):
        EnrollRequestIn()

    parsed = EnrollRequestIn(student_id=42)
    assert parsed.student_id == 42


def test_enroll_request_out_serializes_request_id_as_string():
    request_id = uuid.uuid4()
    out = EnrollRequestOut(request_id=request_id)
    assert out.model_dump(mode="json") == {"request_id": str(request_id)}


def test_status_out_serializes_status_as_plain_string():
    request_id = uuid.uuid4()
    out = StatusOut(request_id=request_id, student_id=1, course_id=2, status=RequestStatus.PENDING)
    dumped = out.model_dump(mode="json")
    assert dumped["status"] == "pending"
