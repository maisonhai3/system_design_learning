"""init

Revision ID: 0001
Revises: 
Create Date: 2026-08-17 14:13:41.676519

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create request_status enum type
    request_status_enum = sa.Enum('pending', 'processing', 'completed', 'rejected_full', name='request_status')
    request_status_enum.create(op.get_bind(), checkfirst=True)

    # Create students table
    op.create_table(
        'students',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('email', sa.String(255), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )

    # Create courses table
    op.create_table(
        'courses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('capacity', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    # Create enrollments table
    op.create_table(
        'enrollments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'course_id', name='uq_enrollment_student_course'),
    )

    # Create enrollment_requests table
    op.create_table(
        'enrollment_requests',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('pending', 'processing', 'completed', 'rejected_full', name='request_status'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['students.id'], ),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop enrollment_requests table
    op.drop_table('enrollment_requests')

    # Drop enrollments table
    op.drop_table('enrollments')

    # Drop courses table
    op.drop_table('courses')

    # Drop students table
    op.drop_table('students')

    # Drop request_status enum
    request_status_enum = sa.Enum('pending', 'processing', 'completed', 'rejected_full', name='request_status')
    request_status_enum.drop(op.get_bind(), checkfirst=True)
