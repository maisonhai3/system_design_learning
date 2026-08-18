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
