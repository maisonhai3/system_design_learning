"""The contracts the use cases are written against.

Dependency inversion, and the reason it is worth the extra file: the use case
declares what it needs, the adapter satisfies it, and the arrow points inward.
`app/usecases/` imports this module; it never imports SQLAlchemy or redis.

Protocol rather than ABC because these are structural: an adapter satisfies the
contract by having the right methods, without importing the protocol or
inheriting from it. That keeps the adapter free of a compile-time dependency on
the domain, and it means a test fake is just a class with three methods.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.entities import Dataset, User


class UserRepository(Protocol):
    async def get(self, user_id: int) -> User | None: ...

    async def set_role(self, user_id: int, role: str) -> User | None:
        """Update and return the row, INCLUDING its new version.

        Returning the version matters: `UPDATE ... RETURNING version` gets it in
        the same round trip and the same transaction, so there is no window in
        which the caller could read a version that belongs to somebody else's
        write.
        """
        ...


class DatasetRepository(Protocol):
    async def visible_to(self, user_id: int, org_id: int) -> list[Dataset]:
        """The ABAC read: rows this subject is allowed to see.

        The filter is in the SQL, not in Python after the fact. Filtering after
        fetching means the rows were on the wire, in the ORM identity map, and
        one `logger.debug(rows)` away from the log aggregator.
        """
        ...


class CacheGateway(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_seconds: int | None) -> None: ...
    async def delete(self, *keys: str) -> int: ...
    async def incr(self, key: str) -> int: ...

    async def advance_pointer(self, key: str, version: int, ttl_seconds: int | None) -> bool:
        """Set `key` to `version` only if it is not already at or beyond it.

        In the port rather than buried in the adapter because it is a
        CONTRACT: "this pointer never moves backwards". An adapter that
        implemented it as GET-then-SET would satisfy the signature and violate
        the contract, which is exactly the race in scenario 03. The Redis
        adapter uses a Lua script; an in-memory fake uses a lock.
        """
        ...
