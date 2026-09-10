"""Composition root: the only module allowed to know both FastAPI and the adapters.

Everything below this file is framework-free. Everything the framework needs to
know about wiring is here. If you ever want to check whether the architecture
has held, grep for `fastapi` outside `app/api/` — the answer should be nothing.
"""

from __future__ import annotations

import os
from typing import Annotated, AsyncIterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.postgres import (
    SqlAlchemyDatasetRepository,
    SqlAlchemyGrantRepository,
    SqlAlchemyUserRepository,
)
from app.adapters.redis_cache import RedisCacheGateway
from app.usecases.get_user import GetUser
from app.usecases.list_datasets import ListVisibleDatasets
from app.usecases.update_role import UpdateRole

DSN = os.environ.get(
    "LAB_ASYNC_DSN",
    os.environ.get("LAB_DSN", "postgresql://lab:lab@localhost:5436/lab").replace(
        "postgresql://", "postgresql+asyncpg://"
    ),
)
REDIS_URL = os.environ.get("LAB_REDIS_URL", "redis://localhost:6380/0")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_cache(request: Request) -> RedisCacheGateway:
    return request.app.state.cache


SessionDep = Annotated[AsyncSession, Depends(get_session)]
CacheDep = Annotated[RedisCacheGateway, Depends(get_cache)]


def get_user_uc(session: SessionDep, cache: CacheDep) -> GetUser:
    return GetUser(SqlAlchemyUserRepository(session), cache)


def update_role_uc(session: SessionDep, cache: CacheDep) -> UpdateRole:
    return UpdateRole(SqlAlchemyUserRepository(session), cache)


def list_datasets_uc(session: SessionDep, cache: CacheDep) -> ListVisibleDatasets:
    return ListVisibleDatasets(SqlAlchemyDatasetRepository(session), cache)


def grants_repo(session: SessionDep) -> SqlAlchemyGrantRepository:
    return SqlAlchemyGrantRepository(session)


async def current_subject(x_user_id: Annotated[int | None, Header()] = None) -> int:
    """The acting user, from a header.

    This is NOT authentication, and the shortcut is deliberate: a real service
    would validate a JWT here and take the subject from a verified claim. The
    lab is about what you do with the subject once you have it, so it is handed
    over rather than proved.

    The distinction the job description asks you to make, in one line:
      AUTHENTICATION answers "who is this" — a signature check, once, at the edge.
      AUTHORIZATION  answers "may they do this to this row" — a policy check,
                     every time, in the service, against data the edge cannot see.
    Only the second one is what this lab caches, and only the second one leaks
    data when you cache it under the wrong key.
    """
    if x_user_id is None:
        raise HTTPException(401, "send X-User-Id: 1 (Alice), 2 (Bob), 3 (Carol) or 4 (Dan)")
    return x_user_id
