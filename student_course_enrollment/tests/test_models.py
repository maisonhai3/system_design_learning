from sqlalchemy import UniqueConstraint

from app.models import Enrollment, EnrollmentRequest, RequestStatus


def test_enrollment_has_unique_student_course_constraint():
    unique_constraints = [c for c in Enrollment.__table__.constraints if isinstance(c, UniqueConstraint)]
    assert len(unique_constraints) == 1
    column_names = {col.name for col in unique_constraints[0].columns}
    assert column_names == {"student_id", "course_id"}


def test_enrollment_request_has_expected_columns():
    columns = {col.name for col in EnrollmentRequest.__table__.columns}
    assert columns == {"id", "student_id", "course_id", "status", "created_at"}
    assert "published_at" not in columns


def test_request_status_values():
    assert {s.value for s in RequestStatus} == {"pending", "processing", "completed", "rejected_full"}
