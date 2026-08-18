import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EnrollmentRequest, RequestStatus


async def create_enrollment_request(session: AsyncSession, student_id: int, course_id: int) -> EnrollmentRequest:
    """Insert a new enrollment_requests row (status=PENDING) and return it.

    Does not commit - the caller is responsible for committing the session.
    """
    raise NotImplementedError


async def get_enrollment_request(session: AsyncSession, request_id: uuid.UUID) -> EnrollmentRequest | None:
    """Fetch an enrollment_requests row by its id, or None if it doesn't exist."""
    raise NotImplementedError


async def try_enroll_student(
    session: AsyncSession, request_id: uuid.UUID, student_id: int, course_id: int
) -> RequestStatus:
    """Check course capacity and insert into enrollments if there's room.

    Must rely on the unique constraint on (student_id, course_id) in the
    enrollments table for idempotency: if a row already exists for this
    student/course pair (e.g. this is a redelivered message), treat it as
    already completed rather than raising.

    Returns RequestStatus.COMPLETED if the student ends up enrolled (or was
    already enrolled), or RequestStatus.REJECTED_FULL if the course is at
    capacity. Does not commit - the caller commits the session.
    """
    raise NotImplementedError


async def mark_request_status(session: AsyncSession, request_id: uuid.UUID, status: RequestStatus) -> None:
    """Update the status column of an enrollment_requests row.

    Does not commit - the caller is responsible for committing the session.
    """
    raise NotImplementedError
