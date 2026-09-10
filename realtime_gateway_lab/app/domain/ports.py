"""The contracts the use cases are written against.

Protocols rather than ABCs: an adapter satisfies the contract structurally, by
having the right methods, so it never imports the domain and the arrow only
points inward. A test fake is a class with three methods and no base class.
"""

from __future__ import annotations

from typing import AsyncContextManager, AsyncIterator, Protocol

from app.domain.entities import Dataset, User


class UserRepository(Protocol):
    async def get(self, user_id: int) -> User | None: ...
    async def all_ids(self) -> list[int]: ...


class DatasetRepository(Protocol):
    async def get(self, dataset_id: int) -> Dataset | None: ...

    async def visible_to(self, user_id: int, org_id: int) -> list[Dataset]: ...

    async def subjects_allowed_to_see(self, dataset_id: int) -> list[int]:
        """Every user allowed to see this dataset, right now.

        This is the query fan-out-on-write needs and filter-on-read never does,
        and it is worth noticing that it runs the authorization policy
        BACKWARDS: instead of "may this subject see this row", it asks "which
        subjects may see this row". Every policy engine can answer the first
        question. Not every one can answer the second, and if yours cannot,
        fan-out on write is not available to you at any price.
        """
        ...


class DatasetRepositoryFactory(Protocol):
    """Hands out a SHORT-LIVED dataset repository, for code that runs for hours.

    A long-lived connection must not hold a short-lived resource. An SSE
    handler lives for as long as the user leaves the tab open; if it holds a
    pooled database session for that whole time, then N concurrent streams
    consume N connections and the pool is exhausted by idle users who are doing
    nothing at all. Worse, each of those sessions keeps a transaction open, so
    a migration cannot take its lock and `DROP TABLE` hangs behind a browser
    tab in another country.

    So the streaming use case receives a FACTORY and opens a session only for
    the microseconds it is actually querying. See scenario 06 for the
    measurement, and note that the fix is a type signature, not a tuning knob.
    """

    def __call__(self) -> AsyncContextManager[DatasetRepository]: ...


class EventLog(Protocol):
    """A durable, ordered, resumable log. Redis Streams in this lab."""

    async def append(self, stream: str, fields: dict[str, str], maxlen: int | None) -> str:
        """Append and return the assigned id. The id IS the SSE cursor."""
        ...

    async def read(
        self, stream: str, after_id: str, block_ms: int, count: int
    ) -> list[tuple[str, dict[str, str]]]:
        """Entries strictly AFTER `after_id`. Blocks up to `block_ms`."""
        ...

    async def length(self, stream: str) -> int: ...

    async def put_body(self, key: str, payload: str) -> None:
        """Store an event body once, referenced by id from many mailboxes.

        On the log rather than on a separate cache port because its lifetime is
        the log's lifetime: a body outlived by the pointers that name it is a
        dangling reference, and one outliving them is a leak. Same owner, same
        retention decision.
        """
        ...

    async def get_body(self, key: str) -> str | None: ...


class DeliveryLog(Protocol):
    async def record(self, event_id: str, user_id: int, dataset_id: int, reason: str) -> None: ...


class Broadcast(Protocol):
    """A fire-and-forget bus. Redis Pub/Sub in this lab.

    Present only so scenario 01 can prove why it is the wrong tool here. It is
    the right tool for plenty of other things — cache-invalidation notices,
    "config changed, reload" — where missing a message is survivable because
    the next one repairs it.
    """

    async def publish(self, channel: str, message: str) -> int: ...
    def subscribe(self, channel: str) -> AsyncIterator[str]: ...
