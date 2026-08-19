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

Note: `worker` and `outbox-relay` will exit immediately with
`NotImplementedError` until their respective stubs are implemented —
that's expected, not a bug.

## Seeding test data

There's no API for creating students/courses (out of scope for this scaffold). Insert them directly:

    docker compose exec postgres psql -U enrollment -d enrollment -c \
      "INSERT INTO students (id, name, email) VALUES (1, 'Ada Lovelace', 'ada@example.com');"
    docker compose exec postgres psql -U enrollment -d enrollment -c \
      "INSERT INTO courses (id, name, capacity) VALUES (1, 'Intro to Systems Design', 1);"

A capacity of 1 lets you demonstrate both the success path and the `rejected_full` path (enroll a second student against the same course).

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
