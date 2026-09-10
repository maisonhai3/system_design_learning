"""Write path — and the shape of the answer to "where does the DEL go?"."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.domain.entities import Invalidation, User
from app.domain.ports import CacheGateway, UserRepository
from app.usecases import keys


class Mode(str, Enum):
    """Switchable so you can break it on purpose. Only two are defensible."""

    DEL_BEFORE_COMMIT = "del_before_commit"  # scenario 01's bug
    DEL_AFTER_COMMIT = "del_after_commit"  # the standard answer
    VERSIONED = "versioned"  # scenario 03's fix
    NO_INVALIDATE = "no_invalidate"  # scenario 02: the DEL that never ran


@dataclass
class WriteResult:
    user: User
    invalidation: Invalidation
    mode: Mode


class UpdateRole:
    """Returns an Invalidation; does not schedule it.

    This is the part the usual advice gets slightly wrong. "Keep BackgroundTasks
    in the router" is right — the router owns the framework. But if the router
    also decides WHICH keys to delete, then every caller has to know the cache
    layout, and the cronjob that forgets is scenario 01 with a different entry
    point.

    Split it: the use case knows WHAT (domain), the caller knows HOW (framework).
    """

    def __init__(self, users: UserRepository, cache: CacheGateway):
        self.users = users
        self.cache = cache

    async def __call__(self, user_id: int, new_role: str, mode: Mode) -> WriteResult | None:
        key = keys.user_profile(user_id)

        if mode is Mode.DEL_BEFORE_COMMIT:
            # WRONG, on purpose. The repository commits inside set_role, so
            # deleting here is deleting while Postgres still serves the old row.
            await self.cache.delete(key)
            user = await self.users.set_role(user_id, new_role)
            if user is None:
                return None
            return WriteResult(user, Invalidation((), "already deleted (too early)"), mode)

        user = await self.users.set_role(user_id, new_role)
        if user is None:
            return None

        if mode is Mode.NO_INVALIDATE:
            return WriteResult(user, Invalidation((), "deliberately dropped"), mode)

        if mode is Mode.VERSIONED:
            # Publish rather than delete: the new version becomes reachable and
            # every older one becomes unreachable at once.
            import json

            await self.cache.set(
                keys.user_profile_at(user_id, user.version),
                json.dumps(user.__dict__),
                keys.DEFAULT_TTL,
            )
            await self.cache.advance_pointer(key, user.version, keys.DEFAULT_TTL)
            return WriteResult(user, Invalidation((), f"published version {user.version}"), mode)

        return WriteResult(user, Invalidation((key,), "delete after commit"), mode)
