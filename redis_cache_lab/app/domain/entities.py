"""Domain entities. Plain data, no framework, no I/O, no imports from anywhere.

The test for whether this layer is honest: could you `python -c "import
app.domain.entities"` with Postgres down, Redis down, FastAPI uninstalled? If
not, something leaked in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str
    role: str
    org_id: int
    version: int  # bumped by a database trigger on every UPDATE


@dataclass(frozen=True)
class Dataset:
    id: int
    org_id: int
    name: str
    classification: str


@dataclass(frozen=True)
class Invalidation:
    """What a write says must be invalidated — not the act of invalidating it.

    This is the piece the usual "put BackgroundTasks in the router" advice
    leaves out. If the router decides WHAT to invalidate, then every caller of
    the use case has to know the cache layout: the HTTP route, the cronjob, the
    Kafka consumer, the admin command. One of them will forget, and the bug it
    produces is scenario 01.

    So the use case returns the intent — it is domain knowledge, it belongs
    with the write — and the caller decides HOW to carry it out: deferred via
    BackgroundTasks in a request, inline in a batch job, through an outbox when
    it must not be lost. Knowledge in the layer that owns it, scheduling in the
    layer that has a scheduler.
    """

    keys: tuple[str, ...]
    reason: str
