# Student Course Enrollment — Resilient Architecture

Date: 2026-08-16
Location: `student_course_enrollment/`

## Purpose

A student course enrollment system that survives the failure of any single
component, demonstrating six resilience patterns in one working project:

| Fire scenario | Mitigation |
|---|---|
| Database overload (registration rush) | Message queue absorbs writes; API never writes to `enrollments` synchronously |
| Polling storms | SSE + Redis Pub/Sub push updates instead of clients polling |
| API server crash | Client reconnects; all state lives in Postgres/Redis, not server memory |
| Worker crash | Queue redelivers unacked messages; unique DB constraint makes reprocessing idempotent |
| Message queue down | API falls back to a local disk spool; a relay retries publishing later |
| Redis down | Timeouts fall back to querying Postgres as source of truth |

## Stack

- Python, FastAPI
- Postgres, SQLAlchemy 2.0 (async) ORM, Alembic migrations
- Redis (Pub/Sub + KV cache)
- RabbitMQ (AMQP, via `aio-pika` or `pika`)
- Docker Compose for local dev

## Code-ownership split

The user writes the bodies of all functions that talk to Postgres, Redis,
RabbitMQ, or the local disk spool. Claude scaffolds everything else
(FastAPI routers, ORM models, Alembic setup, worker/relay loop control
flow, SSE wiring, Docker Compose, project config) as complete, runnable
code that calls into these stubs.

Stub functions (empty body + docstring, to be implemented by the user):

- `app/db.py`
  - `create_enrollment_request(session, student_id, course_id) -> EnrollmentRequest`
  - `get_enrollment_request(session, request_id) -> EnrollmentRequest | None`
  - `try_enroll_student(session, request_id, student_id, course_id) -> EnrollmentResult` — capacity check + insert into `enrollments`, relies on the unique constraint on `(student_id, course_id)` for idempotency
  - `mark_request_status(session, request_id, status) -> None`
- `app/redis_client.py`
  - `cache_status(request_id, status, ttl_seconds) -> None`
  - `get_cached_status(request_id) -> str | None`
  - `publish_status(request_id, status) -> None`
  - `subscribe_status(request_id) -> AsyncIterator[str]`
- `app/mq.py`
  - `publish_request(request_id) -> bool` — returns False (does not raise) on failure so callers can fall back to spooling
  - `consume_requests(callback) -> None` — long-running consume loop, invokes `callback(request_id, ack, nack)` per message
- `app/outbox.py`
  - `spool_write(request_id, payload) -> None`
  - `spool_read_all() -> list[tuple[str, dict]]`
  - `spool_remove(request_id) -> None`

## Data model

- `students`: id, name, email
- `courses`: id, name, capacity
- `enrollments`: id, student_id (FK), course_id (FK), created_at; **unique
  constraint on `(student_id, course_id)`** — the idempotency guard for
  redelivered messages
- `enrollment_requests`: id (UUID = request_id), student_id, course_id,
  status (`pending` / `processing` / `completed` / `rejected_full`),
  created_at — source of truth for status polling and the Redis-down
  fallback. No `published_at` column: whether a request still needs
  publishing is determined by the presence of a spool file, not a DB flag.

## Request & notification flow

1. `POST /courses/{course_id}/enroll` (body: `student_id`): API creates an
   `enrollment_requests` row (`status=pending`), then calls
   `mq.publish_request(request_id)`. If it returns `False` (RabbitMQ
   unreachable), falls back to `outbox.spool_write(request_id, payload)`.
   Returns `202 {request_id}` immediately.
2. The **outbox-relay** process loops: `outbox.spool_read_all()`, retries
   `mq.publish_request` for each, and on success calls
   `outbox.spool_remove(request_id)`.
3. The **worker** process runs `mq.consume_requests(callback)`. For each
   message, the callback loads the request, calls
   `db.try_enroll_student(...)` inside a transaction, then
   `db.mark_request_status(...)`. Only acks after the transaction commits
   — a crash before ack means redelivery, and redelivery after an
   already-committed enrollment just hits the unique constraint and is
   treated as already-done (idempotent).
4. After committing, the worker calls `redis_client.cache_status(...)` (TTL
   cache, catches a late SSE connect) and `redis_client.publish_status(...)`
   (Pub/Sub, catches a live SSE connection).
5. `GET /enrollments/requests/{request_id}/stream` (SSE): on connect,
   checks `get_cached_status` first (catches a status that already fired),
   then `subscribe_status` for further updates. If a Redis call times out,
   falls back to a short poll loop against `GET
   /enrollments/requests/{request_id}` (Postgres) instead.
6. `GET /enrollments/requests/{request_id}`: plain status endpoint,
   queries `enrollment_requests` in Postgres directly. This is also what
   the SSE fallback polls.

Client reconnect after an API crash is just a new SSE connection against
the same `request_id` — no server-side session state to lose.

## Project layout

```
student_course_enrollment/
├── docker-compose.yml          # postgres, redis, rabbitmq, api, worker, outbox-relay
├── Dockerfile
├── pyproject.toml
├── alembic/ + alembic.ini
├── spool/                      # local disk spool, bind-mounted into api + outbox-relay
├── app/
│   ├── main.py                 # FastAPI app, routers
│   ├── config.py               # env-based settings
│   ├── models.py               # SQLAlchemy ORM models
│   ├── schemas.py               # Pydantic request/response models
│   ├── api/
│   │   └── enrollments.py      # POST enroll, GET status, GET stream (SSE)
│   ├── db.py                   # STUB
│   ├── redis_client.py         # STUB
│   ├── mq.py                   # STUB
│   ├── outbox.py               # STUB
│   ├── worker.py               # worker main loop (complete, calls stubs)
│   └── outbox_relay.py         # relay loop (complete, calls stubs)
└── README.md                   # how to run, which files are stubs
```

Docker Compose services: `postgres`, `redis`, `rabbitmq` (management
plugin enabled), `api`, `worker`, `outbox-relay`. App services depend on
the three infra containers; `api` and `outbox-relay` share a bind-mounted
`./spool` directory.

## Testing

Given the code-ownership split, automated tests for the stubbed
functions aren't meaningful until the user fills them in. The scaffold
includes a README section describing how to manually exercise the full
flow via `docker compose up` and `curl`/browser SSE, plus a note on where
the user could add their own unit tests once the stubs are implemented.

## Out of scope

- Authentication/authorization
- Production deployment (K8s, TLS, secrets management)
- Multi-instance RabbitMQ/Redis/Postgres clustering
- Dead-letter queue handling beyond a basic nack/requeue
