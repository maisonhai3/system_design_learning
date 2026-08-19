import uuid

from pydantic import BaseModel

from app.models import RequestStatus


class EnrollRequestIn(BaseModel):
    student_id: int


class EnrollRequestOut(BaseModel):
    request_id: uuid.UUID


class StatusOut(BaseModel):
    request_id: uuid.UUID
    student_id: int
    course_id: int
    status: RequestStatus
