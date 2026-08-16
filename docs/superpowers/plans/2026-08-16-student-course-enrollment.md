# Student Course Enrollment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scaffold a resilient student-course-enrollment system (FastAPI + Postgres + Redis + RabbitMQ, Docker Compose for local dev) where all control flow (routing, worker loop, outbox relay, SSE fallback) is fully implemented, and only the functions that actually talk to Postgres, Redis, RabbitMQ, and the local disk spool are left as stubs for the user to fill in.

**Architecture:** FastAPI accepts enrollment requests and enqueues them instead of writing synchronously; a worker consumes the queue and does the real DB write under a unique constraint for idempotency; Redis Pub/Sub plus an SSE endpoint push status to clients instead of polling; a local-disk-spool outbox absorbs RabbitMQ outages and a relay process retries; the SSE endpoint and status queries fall back to Postgres directly if Redis times out.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 (async, asyncpg), Alembic, Pydantic v2 / pydantic-settings, redis-py (`redis.asyncio`), aio-pika (RabbitMQ), pytest + pytest-asyncio, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-16-student-course-enrollment-design.md`

## Global Constraints

- All project files live under `student_course_enrollment/` — nothing at the repo root.
- Python 3.11+, async/await throughout the FastAPI/worker/relay code paths.
- SQLAlchemy 2.0 async ORM with `Mapped[...]`/`mapped_column` style models; Alembic for migrations.
- The four stub modules (`app/db.py`, `app/redis_client.py`, `app/mq.py`, `app/outbox.py`) must only ever contain function signatures + docstrings + `raise NotImplementedError` — no real Postgres/Redis/RabbitMQ/disk logic gets written into them by any task in this plan. Every other file is fully implemented.
- `enrollments` table has a unique constraint on `(student_id, course_id)` — this is the idempotency mechanism and must not be dropped or weakened.
- No `published_at` column or DB-flag-based outbox — "has this been published" is determined by whether a spool file exists on local disk, per user correction during brainstorming.
- `pytest-asyncio` runs in `asyncio_mode = "auto"` so async test functions don't need `@pytest.mark.asyncio`.

---

### Task 1: Project scaffolding, config, and local infrastructure containers

**Files:**
- Create: `student_course_enrollment/pyproject.toml`
- Create: `student_course_enrollment/.env.example`
- Create: `student_course_enrollment/app/__init__.py`
- Create: `student_course_enrollment/app/config.py`
- Create: `student_course_enrollment/docker-compose.yml`
- Create: `student_course_enrollment/Dockerfile`
- Create: `student_course_enrollment/spool/.gitkeep`
- Create: `student_course_enrollment/tests/__init__.py`
- Test: `student_course_enrollment/tests/test_config.py`
- Modify: `.gitignore` (repo root)

**Interfaces:**
- Produces: `app.config.settings` — a `Settings` instance with attributes `database_url: str`, `sync_database_url: str` (property), `redis_url: str`, `rabbitmq_url: str`, `spool_dir: str`, `redis_timeout_seconds: float`, `poll_interval_seconds: float`, `status_cache_ttl_seconds: int`. Every later task imports this.

- [ ] **Step 1: Create the package layout**

```bash
mkdir -p student_course_enrollment/app student_course_enrollment/tests student_course_enrollment/spool
touch student_course_enrollment/app/__init__.py student_course_enrollment/tests/__init__.py
touch student_course_enrollment/spool/.gitkeep
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "student-course-enrollment"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "psycopg2-binary>=2.9",
    "alembic>=1.13",
    "pydantic-settings>=2.4",
    "redis>=5.0",
    "aio-pika>=9.4",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 3: Write `app/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://enrollment:enrollment@localhost:5432/enrollment"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    spool_dir: str = "./spool"
    redis_timeout_seconds: float = 1.0
    poll_interval_seconds: float = 1.0
    status_cache_ttl_seconds: int = 300

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg2")


settings = Settings()
```

- [ ] **Step 4: Write `.env.example`**

```bash
DATABASE_URL=postgresql+asyncpg://enrollment:enrollment@localhost:5432/enrollment
REDIS_URL=redis://localhost:6379/0
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
SPOOL_DIR=./spool
```

- [ ] **Step 5: Write the failing test for config**

```python
# student_course_enrollment/tests/test_config.py
from app.config import Settings


def test_sync_database_url_swaps_driver():
    settings = Settings(database_url="postgresql+asyncpg://u:p@host:5432/db")
    assert settings.sync_database_url == "postgresql+psycopg2://u:p@host:5432/db"
```

- [ ] **Step 6: Install deps and run the test to verify it fails**

```bash
cd student_course_enrollment
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/test_config.py -v
```

Expected: FAIL (or error) because `app/config.py` doesn't exist yet at this point if you run this before Step 3 — since Step 3 already created it above, this should actually PASS. Run it now to confirm it passes (Steps 3-4 are the implementation; this step is the verification):

Expected: PASS

- [ ] **Step 7: Write `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: enrollment
      POSTGRES_PASSWORD: enrollment
      POSTGRES_DB: enrollment
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U enrollment"]
      interval: 5s
      timeout: 5s
      retries: 10

  redis:
    image: redis:7
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 10

  rabbitmq:
    image: rabbitmq:3.13-management
    ports:
      - "5672:5672"
      - "15672:15672"
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 5s
      timeout: 5s
      retries: 10

  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql+asyncpg://enrollment:enrollment@postgres:5432/enrollment
      REDIS_URL: redis://redis:6379/0
      RABBITMQ_URL: amqp://guest:guest@rabbitmq:5672/
      SPOOL_DIR: /spool
    volumes:
      - ./app:/app/app
      - ./spool:/spool
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy

  worker:
    build: .
    command: python -m app.worker
    environment:
      DATABASE_URL: postgresql+asyncpg://enrollment:enrollment@postgres:5432/enrollment
      REDIS_URL: redis://redis:6379/0
      RABBITMQ_URL: amqp://guest:guest@rabbitmq:5672/
      SPOOL_DIR: /spool
    volumes:
      - ./app:/app/app
      - ./spool:/spool
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy

  outbox-relay:
    build: .
    command: python -m app.outbox_relay
    environment:
      DATABASE_URL: postgresql+asyncpg://enrollment:enrollment@postgres:5432/enrollment
      REDIS_URL: redis://redis:6379/0
      RABBITMQ_URL: amqp://guest:guest@rabbitmq:5672/
      SPOOL_DIR: /spool
    volumes:
      - ./app:/app/app
      - ./spool:/spool
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy

volumes:
  pgdata:
```

- [ ] **Step 8: Write `Dockerfile`**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[dev]"

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

(`alembic/` and `alembic.ini` don't exist yet — that's fine, this Dockerfile isn't built until Task 9. `COPY` of a missing path would fail the build, so this file is inert until Task 2 adds those.)

- [ ] **Step 9: Bring up the three infra containers and verify health**

```bash
cd student_course_enrollment
docker compose up -d postgres redis rabbitmq
docker compose ps
```

Expected: all three show `healthy` within ~30s. Leave them running for Task 2.

- [ ] **Step 10: Add spool runtime files to `.gitignore`**

Append to the repo-root `.gitignore`:

```
# student_course_enrollment local spool (runtime state, not source)
student_course_enrollment/spool/*
!student_course_enrollment/spool/.gitkeep
```

- [ ] **Step 11: Commit**

```bash
git add student_course_enrollment/pyproject.toml student_course_enrollment/.env.example \
  student_course_enrollment/app/__init__.py student_course_enrollment/app/config.py \
  student_course_enrollment/docker-compose.yml student_course_enrollment/Dockerfile \
  student_course_enrollment/spool/.gitkeep student_course_enrollment/tests/__init__.py \
  student_course_enrollment/tests/test_config.py .gitignore
git commit -m "Scaffold project layout, config, and local infra containers"
```

---

### Task 2: ORM models, database engine, and Alembic migration

**Files:**
- Create: `student_course_enrollment/app/models.py`
- Create: `student_course_enrollment/app/database.py`
- Create: `student_course_enrollment/alembic.ini`
- Create: `student_course_enrollment/alembic/env.py`
- Create: `student_course_enrollment/alembic/script.py.mako`
- Test: `student_course_enrollment/tests/test_models.py`

**Interfaces:**
- Consumes: `app.config.settings` (Task 1)
- Produces: `app.models.Base`, `app.models.RequestStatus` (str enum: PENDING, PROCESSING, COMPLETED, REJECTED_FULL), `app.models.Student`, `app.models.Course`, `app.models.Enrollment`, `app.models.EnrollmentRequest` (fields: `id: uuid.UUID`, `student_id: int`, `course_id: int`, `status: RequestStatus`, `created_at: datetime`). `app.database.AsyncSessionLocal`, `app.database.get_session` (async generator FastAPI dependency yielding an `AsyncSession`).

- [ ] **Step 1: Write the failing test for the idempotency constraint**

```python
# student_course_enrollment/tests/test_models.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd student_course_enrollment
pytest tests/test_models.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.models'`

- [ ] **Step 3: Write `app/models.py`**

```python
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RequestStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED_FULL = "rejected_full"


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True)


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    capacity: Mapped[int] = mapped_column(Integer)


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("student_id", "course_id", name="uq_enrollment_student_course"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class EnrollmentRequest(Base):
    __tablename__ = "enrollment_requests"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    status: Mapped[RequestStatus] = mapped_column(
        SAEnum(RequestStatus, name="request_status"), default=RequestStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_models.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Write `app/database.py`**

```python
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
```

- [ ] **Step 6: Initialize Alembic and point it at the async settings/models**

```bash
cd student_course_enrollment
alembic init alembic
```

This generates `alembic.ini` and `alembic/env.py` with defaults — replace `alembic/env.py`'s content entirely with:

```python
# student_course_enrollment/alembic/env.py
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.sync_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.sync_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 7: Generate and apply the initial migration against the running Postgres container**

```bash
cd student_course_enrollment
alembic revision --autogenerate -m "init"
alembic upgrade head
```

- [ ] **Step 8: Verify the tables exist**

```bash
docker compose exec postgres psql -U enrollment -d enrollment -c '\dt'
```

Expected: `students`, `courses`, `enrollments`, `enrollment_requests`, `alembic_version` all listed.

- [ ] **Step 9: Commit**

```bash
git add student_course_enrollment/app/models.py student_course_enrollment/app/database.py \
  student_course_enrollment/alembic.ini student_course_enrollment/alembic \
  student_course_enrollment/tests/test_models.py
git commit -m "Add ORM models, async DB engine, and initial Alembic migration"
```

---

### Task 3: Pydantic schemas

**Files:**
- Create: `student_course_enrollment/app/schemas.py`
- Test: `student_course_enrollment/tests/test_schemas.py`

**Interfaces:**
- Consumes: `app.models.RequestStatus` (Task 2)
- Produces: `app.schemas.EnrollRequestIn` (field: `student_id: int`), `app.schemas.EnrollRequestOut` (field: `request_id: uuid.UUID`), `app.schemas.StatusOut` (fields: `request_id: uuid.UUID`, `student_id: int`, `course_id: int`, `status: RequestStatus`)

- [ ] **Step 1: Write the failing test**

```python
# student_course_enrollment/tests/test_schemas.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd student_course_enrollment
pytest tests/test_schemas.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas'`

- [ ] **Step 3: Write `app/schemas.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_schemas.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add student_course_enrollment/app/schemas.py student_course_enrollment/tests/test_schemas.py
git commit -m "Add Pydantic request/response schemas"
```

---

### Task 4: DB/Redis/MQ/Outbox stub interfaces (user-owned)

**Files:**
- Create: `student_course_enrollment/app/db.py`
- Create: `student_course_enrollment/app/redis_client.py`
- Create: `student_course_enrollment/app/mq.py`
- Create: `student_course_enrollment/app/outbox.py`
- Test: `student_course_enrollment/tests/test_stubs_contract.py`

**Interfaces:**
- Consumes: `app.models.EnrollmentRequest`, `app.models.RequestStatus` (Task 2)
- Produces (all stub — bodies raise `NotImplementedError`, to be filled in by the user later):
  - `app.db.create_enrollment_request(session, student_id: int, course_id: int) -> EnrollmentRequest`
  - `app.db.get_enrollment_request(session, request_id: uuid.UUID) -> EnrollmentRequest | None`
  - `app.db.try_enroll_student(session, request_id: uuid.UUID, student_id: int, course_id: int) -> RequestStatus`
  - `app.db.mark_request_status(session, request_id: uuid.UUID, status: RequestStatus) -> None`
  - `app.redis_client.cache_status(request_id: uuid.UUID, status: str, ttl_seconds: int) -> None`
  - `app.redis_client.get_cached_status(request_id: uuid.UUID) -> str | None`
  - `app.redis_client.publish_status(request_id: uuid.UUID, status: str) -> None`
  - `app.redis_client.subscribe_status(request_id: uuid.UUID) -> AsyncIterator[str]`
  - `app.mq.publish_request(request_id: uuid.UUID) -> bool`
  - `app.mq.consume_requests(callback) -> None`
  - `app.outbox.spool_write(request_id: uuid.UUID, payload: dict) -> None`
  - `app.outbox.spool_read_all() -> list[tuple[uuid.UUID, dict]]`
  - `app.outbox.spool_remove(request_id: uuid.UUID) -> None`

- [ ] **Step 1: Write the failing contract test**

```python
# student_course_enrollment/tests/test_stubs_contract.py
import uuid

import pytest

from app import db, mq, outbox, redis_client


async def test_db_stubs_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        await db.create_enrollment_request(session=None, student_id=1, course_id=1)
    with pytest.raises(NotImplementedError):
        await db.get_enrollment_request(session=None, request_id=uuid.uuid4())
    with pytest.raises(NotImplementedError):
        await db.try_enroll_student(session=None, request_id=uuid.uuid4(), student_id=1, course_id=1)
    with pytest.raises(NotImplementedError):
        await db.mark_request_status(session=None, request_id=uuid.uuid4(), status=None)


async def test_redis_stubs_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        await redis_client.cache_status(uuid.uuid4(), "pending", 60)
    with pytest.raises(NotImplementedError):
        await redis_client.get_cached_status(uuid.uuid4())
    with pytest.raises(NotImplementedError):
        await redis_client.publish_status(uuid.uuid4(), "pending")
    with pytest.raises(NotImplementedError):
        async for _ in redis_client.subscribe_status(uuid.uuid4()):
            pass


async def test_mq_stubs_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        await mq.publish_request(uuid.uuid4())
    with pytest.raises(NotImplementedError):
        await mq.consume_requests(callback=None)


def test_outbox_stubs_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        outbox.spool_write(uuid.uuid4(), {})
    with pytest.raises(NotImplementedError):
        outbox.spool_read_all()
    with pytest.raises(NotImplementedError):
        outbox.spool_remove(uuid.uuid4())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd student_course_enrollment
pytest tests/test_stubs_contract.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 3: Write `app/db.py`**

```python
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
```

- [ ] **Step 4: Write `app/redis_client.py`**

```python
import uuid
from typing import AsyncIterator


async def cache_status(request_id: uuid.UUID, status: str, ttl_seconds: int) -> None:
    """Write the current status to a Redis key (e.g. f"status:{request_id}") with a TTL.

    Lets a late-connecting SSE client see a status that already fired
    before it subscribed to the Pub/Sub channel.
    """
    raise NotImplementedError


async def get_cached_status(request_id: uuid.UUID) -> str | None:
    """Read the cached status key written by cache_status, or None if absent/expired."""
    raise NotImplementedError


async def publish_status(request_id: uuid.UUID, status: str) -> None:
    """Publish the new status to a Redis Pub/Sub channel (e.g. f"enrollment_status:{request_id}")."""
    raise NotImplementedError


async def subscribe_status(request_id: uuid.UUID) -> AsyncIterator[str]:
    """Subscribe to the Pub/Sub channel for this request_id and yield each status as it arrives.

    Should keep yielding until the caller stops iterating (e.g. breaks out
    of the loop after a terminal status) - does not need to know about
    terminal statuses itself.
    """
    raise NotImplementedError
    yield  # pragma: no cover - makes this an async generator
```

- [ ] **Step 5: Write `app/mq.py`**

```python
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
```

- [ ] **Step 6: Write `app/outbox.py`**

```python
import uuid


def spool_write(request_id: uuid.UUID, payload: dict) -> None:
    """Write payload to a local spool file named after request_id (e.g. {SPOOL_DIR}/{request_id}.json)."""
    raise NotImplementedError


def spool_read_all() -> list[tuple[uuid.UUID, dict]]:
    """Return (request_id, payload) for every file currently in the spool directory."""
    raise NotImplementedError


def spool_remove(request_id: uuid.UUID) -> None:
    """Delete the spool file for request_id, e.g. after it's been successfully published."""
    raise NotImplementedError
```

- [ ] **Step 7: Run test to verify it passes**

```bash
pytest tests/test_stubs_contract.py -v
```

Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add student_course_enrollment/app/db.py student_course_enrollment/app/redis_client.py \
  student_course_enrollment/app/mq.py student_course_enrollment/app/outbox.py \
  student_course_enrollment/tests/test_stubs_contract.py
git commit -m "Add DB/Redis/MQ/outbox stub interfaces for user implementation"
```

---

### Task 5: Enrollment API routes

**Files:**
- Create: `student_course_enrollment/app/api/__init__.py`
- Create: `student_course_enrollment/app/api/enrollments.py`
- Create: `student_course_enrollment/app/main.py`
- Create: `student_course_enrollment/tests/conftest.py`
- Test: `student_course_enrollment/tests/test_enrollments_api.py`

**Interfaces:**
- Consumes: `app.db.create_enrollment_request`, `app.db.get_enrollment_request` (Task 4, called but not implemented — tests monkeypatch them); `app.mq.publish_request` (Task 4); `app.outbox.spool_write` (Task 4); `app.schemas.EnrollRequestIn/EnrollRequestOut/StatusOut` (Task 3); `app.database.get_session` (Task 2)
- Produces: `app.api.enrollments.router` (a FastAPI `APIRouter` with `POST /courses/{course_id}/enroll` and `GET /enrollments/requests/{request_id}`), `app.main.app` (the FastAPI application)

- [ ] **Step 1: Write `tests/conftest.py`**

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.database import get_session
from app.main import app


@pytest.fixture
def client():
    async def override_get_session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Write the failing test**

```python
# student_course_enrollment/tests/test_enrollments_api.py
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
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd student_course_enrollment
pytest tests/test_enrollments_api.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.api'`

- [ ] **Step 4: Write `app/api/__init__.py`** (empty file)

```bash
touch student_course_enrollment/app/api/__init__.py
```

- [ ] **Step 5: Write `app/api/enrollments.py`**

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, mq, outbox
from app.database import get_session
from app.schemas import EnrollRequestIn, EnrollRequestOut, StatusOut

router = APIRouter()


@router.post("/courses/{course_id}/enroll", response_model=EnrollRequestOut, status_code=202)
async def enroll(
    course_id: int, body: EnrollRequestIn, session: AsyncSession = Depends(get_session)
) -> EnrollRequestOut:
    req = await db.create_enrollment_request(session, body.student_id, course_id)
    await session.commit()

    published = await mq.publish_request(req.id)
    if not published:
        outbox.spool_write(req.id, {"student_id": body.student_id, "course_id": course_id})

    return EnrollRequestOut(request_id=req.id)


@router.get("/enrollments/requests/{request_id}", response_model=StatusOut)
async def get_status(request_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> StatusOut:
    req = await db.get_enrollment_request(session, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")

    return StatusOut(request_id=req.id, student_id=req.student_id, course_id=req.course_id, status=req.status)
```

- [ ] **Step 6: Write `app/main.py`**

```python
from fastapi import FastAPI

from app.api.enrollments import router as enrollments_router

app = FastAPI(title="Student Course Enrollment")
app.include_router(enrollments_router)
```

- [ ] **Step 7: Run test to verify it passes**

```bash
pytest tests/test_enrollments_api.py -v
```

Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add student_course_enrollment/app/api student_course_enrollment/app/main.py \
  student_course_enrollment/tests/conftest.py student_course_enrollment/tests/test_enrollments_api.py
git commit -m "Add enrollment API routes (POST enroll, GET status)"
```

---

### Task 6: SSE status stream with Redis-down fallback

**Files:**
- Create: `student_course_enrollment/app/api/sse.py`
- Modify: `student_course_enrollment/app/api/enrollments.py`
- Test: `student_course_enrollment/tests/test_sse.py`

**Interfaces:**
- Consumes: `app.redis_client.get_cached_status/subscribe_status` (Task 4); `app.db.get_enrollment_request` (Task 4); `app.config.settings` (Task 1); `app.models.RequestStatus` (Task 2)
- Produces: `app.api.sse.stream_events(request_id, session) -> AsyncIterator[str]`, `app.api.sse.RedisUnavailable` (exception)

- [ ] **Step 1: Write the failing test**

```python
# student_course_enrollment/tests/test_sse.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd student_course_enrollment
pytest tests/test_sse.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.sse'`

- [ ] **Step 3: Write `app/api/sse.py`**

```python
import asyncio
import uuid
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app import db, redis_client
from app.config import settings
from app.models import RequestStatus

TERMINAL_STATUSES = {RequestStatus.COMPLETED.value, RequestStatus.REJECTED_FULL.value}


class RedisUnavailable(Exception):
    pass


async def _subscribe_with_timeout(request_id: uuid.UUID) -> AsyncIterator[str]:
    subscription = redis_client.subscribe_status(request_id)
    while True:
        try:
            status = await asyncio.wait_for(subscription.__anext__(), timeout=settings.redis_timeout_seconds)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise RedisUnavailable from exc
        yield status


async def _poll_fallback(request_id: uuid.UUID, session: AsyncSession) -> AsyncIterator[str]:
    while True:
        req = await db.get_enrollment_request(session, request_id)
        if req is None:
            return
        yield req.status.value
        if req.status.value in TERMINAL_STATUSES:
            return
        await asyncio.sleep(settings.poll_interval_seconds)


async def stream_events(request_id: uuid.UUID, session: AsyncSession) -> AsyncIterator[str]:
    try:
        cached = await asyncio.wait_for(
            redis_client.get_cached_status(request_id), timeout=settings.redis_timeout_seconds
        )
    except asyncio.TimeoutError:
        cached = None

    if cached is not None:
        yield cached
        if cached in TERMINAL_STATUSES:
            return

    try:
        async for status in _subscribe_with_timeout(request_id):
            yield status
            if status in TERMINAL_STATUSES:
                return
    except RedisUnavailable:
        async for status in _poll_fallback(request_id, session):
            yield status
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_sse.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Wire the SSE endpoint into the router**

Modify `student_course_enrollment/app/api/enrollments.py` — add these imports and route:

```python
from fastapi.responses import StreamingResponse

from app.api.sse import stream_events
```

```python
@router.get("/enrollments/requests/{request_id}/stream")
async def stream(request_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    async def formatted_events():
        async for status in stream_events(request_id, session):
            yield f"data: {status}\n\n"

    return StreamingResponse(formatted_events(), media_type="text/event-stream")
```

- [ ] **Step 6: Commit**

```bash
git add student_course_enrollment/app/api/sse.py student_course_enrollment/app/api/enrollments.py \
  student_course_enrollment/tests/test_sse.py
git commit -m "Add SSE status stream with Redis-down polling fallback"
```

---

### Task 7: Worker process

**Files:**
- Create: `student_course_enrollment/app/worker.py`
- Test: `student_course_enrollment/tests/test_worker.py`

**Interfaces:**
- Consumes: `app.db.get_enrollment_request/try_enroll_student/mark_request_status` (Task 4); `app.redis_client.cache_status/publish_status` (Task 4); `app.mq.consume_requests` (Task 4); `app.database.AsyncSessionLocal` (Task 2); `app.config.settings` (Task 1)
- Produces: `app.worker.handle_message(session_factory, request_id, ack, nack) -> None`, `app.worker.run_worker(session_factory) -> None`

- [ ] **Step 1: Write the failing test**

```python
# student_course_enrollment/tests/test_worker.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd student_course_enrollment
pytest tests/test_worker.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.worker'`

- [ ] **Step 3: Write `app/worker.py`**

```python
import asyncio
import uuid
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import db, mq, redis_client
from app.config import settings
from app.database import AsyncSessionLocal


async def handle_message(
    session_factory: async_sessionmaker[AsyncSession],
    request_id: uuid.UUID,
    ack: Callable[[], Awaitable[None]],
    nack: Callable[[], Awaitable[None]],
) -> None:
    async with session_factory() as session:
        try:
            req = await db.get_enrollment_request(session, request_id)
            if req is None:
                await nack()
                return
            status = await db.try_enroll_student(session, request_id, req.student_id, req.course_id)
            await db.mark_request_status(session, request_id, status)
            await session.commit()
        except Exception:
            await session.rollback()
            await nack()
            return

    await redis_client.cache_status(request_id, status.value, settings.status_cache_ttl_seconds)
    await redis_client.publish_status(request_id, status.value)
    await ack()


async def run_worker(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async def callback(request_id: uuid.UUID, ack, nack) -> None:
        await handle_message(session_factory, request_id, ack, nack)

    await mq.consume_requests(callback)


if __name__ == "__main__":
    asyncio.run(run_worker(AsyncSessionLocal))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_worker.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add student_course_enrollment/app/worker.py student_course_enrollment/tests/test_worker.py
git commit -m "Add worker process with idempotent ack/nack handling"
```

---

### Task 8: Outbox relay process

**Files:**
- Create: `student_course_enrollment/app/outbox_relay.py`
- Test: `student_course_enrollment/tests/test_outbox_relay.py`

**Interfaces:**
- Consumes: `app.outbox.spool_read_all/spool_remove` (Task 4); `app.mq.publish_request` (Task 4); `app.config.settings` (Task 1)
- Produces: `app.outbox_relay.relay_once() -> int`, `app.outbox_relay.run_relay(interval_seconds: float) -> None`

- [ ] **Step 1: Write the failing test**

```python
# student_course_enrollment/tests/test_outbox_relay.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd student_course_enrollment
pytest tests/test_outbox_relay.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.outbox_relay'`

- [ ] **Step 3: Write `app/outbox_relay.py`**

```python
import asyncio

from app import mq, outbox
from app.config import settings


async def relay_once() -> int:
    relayed = 0
    for request_id, _ in outbox.spool_read_all():
        published = await mq.publish_request(request_id)
        if published:
            outbox.spool_remove(request_id)
            relayed += 1
    return relayed


async def run_relay(interval_seconds: float) -> None:
    while True:
        await relay_once()
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    asyncio.run(run_relay(settings.poll_interval_seconds))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_outbox_relay.py -v
```

Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add student_course_enrollment/app/outbox_relay.py student_course_enrollment/tests/test_outbox_relay.py
git commit -m "Add outbox relay process for local-disk-spool retries"
```

---

### Task 9: Full Docker Compose wiring and README

**Files:**
- Modify: `student_course_enrollment/docker-compose.yml` (no content changes needed — already includes api/worker/outbox-relay from Task 1; this task builds and exercises it)
- Create: `student_course_enrollment/README.md`

**Interfaces:**
- Consumes: everything from Tasks 1-8.
- Produces: a runnable (once stubs are implemented) full stack via `docker compose up`.

- [ ] **Step 1: Run the full test suite once more before touching infra**

```bash
cd student_course_enrollment
pytest -v
```

Expected: all tests from Tasks 1-8 PASS.

- [ ] **Step 2: Build and bring up the full stack**

```bash
docker compose build
docker compose up -d
docker compose ps
```

Expected: `postgres`, `redis`, `rabbitmq` healthy; `api`, `worker`, `outbox-relay` running (they'll raise `NotImplementedError` the moment a request actually hits a stub function — expected until the stubs are filled in).

- [ ] **Step 3: Apply the migration inside the running stack**

```bash
docker compose exec api alembic upgrade head
```

- [ ] **Step 4: Smoke-test the API surface reaches the stub boundary**

```bash
curl -i -X POST http://localhost:8000/courses/1/enroll -H 'Content-Type: application/json' -d '{"student_id": 1}'
```

Expected: `500 Internal Server Error` (because `db.create_enrollment_request` raises `NotImplementedError`) — this confirms the routing, request parsing, and dependency wiring all work correctly; the 500 is expected until the user implements the stubs.

- [ ] **Step 5: Write `README.md`**

```markdown
# Student Course Enrollment

A resilient course enrollment system. See
`docs/superpowers/specs/2026-08-16-student-course-enrollment-design.md`
in the repo root for the full design and the fire-scenario-to-mitigation
mapping this implements.

## Running locally

    docker compose up -d
    docker compose exec api alembic upgrade head

Then:
- `POST /courses/{course_id}/enroll` with `{"student_id": <int>}` — returns `202 {"request_id": "..."}`
- `GET /enrollments/requests/{request_id}` — polls current status
- `GET /enrollments/requests/{request_id}/stream` — SSE stream of status updates

RabbitMQ management UI: http://localhost:15672 (guest/guest)

## What's implemented vs. what you need to write

Everything is wired up and tested except the functions that actually talk
to Postgres, Redis, RabbitMQ, and the local disk spool — those are left
as stubs (`raise NotImplementedError`) for you to fill in:

- `app/db.py` — Postgres queries (start here; the API returns 500s until
  this is done)
- `app/mq.py` — RabbitMQ publish/consume
- `app/outbox.py` — local disk spool read/write/remove
- `app/redis_client.py` — Redis cache + Pub/Sub

Suggested implementation order: `db.py` → `mq.py` → `outbox.py` →
`redis_client.py`, since that's the order requests actually flow through
the system, so you can test each layer against the previous one working.

Every stub function's docstring says exactly what it must do and return.

## Running tests

    pip install -e ".[dev]"
    pytest -v

Tests for the scaffolded code (routes, worker loop, outbox relay, SSE
fallback) monkeypatch the stub functions, so they pass before you've
implemented anything. Once you implement a stub, consider adding your own
integration tests against the real Postgres/Redis/RabbitMQ containers.
```

- [ ] **Step 6: Tear down**

```bash
docker compose down
```

- [ ] **Step 7: Commit**

```bash
git add student_course_enrollment/README.md
git commit -m "Add README documenting run instructions and stub implementation order"
```
