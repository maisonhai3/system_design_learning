"""Composition root, and the trust boundary.

Everything below this package is framework-free. Everything the framework needs
to know about wiring is here. The grep test for the inner layers:

    grep -rn "fastapi" app/domain app/usecases   # → nothing
"""

from __future__ import annotations

import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.postgres import (
    SqlAlchemyDatasetRepository,
    SqlAlchemyDeliveryLog,
    SqlAlchemyGrantRepository,
    SqlAlchemyUserRepository,
)
from app.domain.entities import Subject
from app.usecases.publish import PublishDatasetEvent
from app.usecases.subscribe import StreamFeed

DSN = os.environ.get(
    "LAB_ASYNC_DSN",
    os.environ.get("LAB_DSN", "postgresql://lab:lab@localhost:5437/lab").replace(
        "postgresql://", "postgresql+asyncpg://"
    ),
)
REDIS_URL = os.environ.get("LAB_REDIS_URL", "redis://localhost:6381/0")

# How this service decides who is asking. The three postures scenario 03 walks.
#
#   client  trust X-User-Id from the caller.       Convenient. A backdoor.
#   gateway trust X-Auth-* injected by the gateway. Correct IF nothing else can
#           reach this service. That "if" is a network fact, not a code fact.
#   signed  trust X-Auth-* only when X-Auth-Signature verifies. Correct even
#           when something else CAN reach the service.
SUBJECT_TRUST = os.environ.get("LAB_SUBJECT_TRUST", "client")
GATEWAY_SECRET = os.environ.get("LAB_GATEWAY_SECRET", "dev-gateway-secret")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_log(request: Request):
    return request.app.state.event_log


def get_broadcast(request: Request):
    return request.app.state.broadcast


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def users_repo(session: SessionDep) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(session)


def datasets_repo(session: SessionDep) -> SqlAlchemyDatasetRepository:
    return SqlAlchemyDatasetRepository(session)


def grants_repo(session: SessionDep) -> SqlAlchemyGrantRepository:
    return SqlAlchemyGrantRepository(session)


def publish_uc(session: SessionDep, request: Request) -> PublishDatasetEvent:
    return PublishDatasetEvent(
        SqlAlchemyDatasetRepository(session),
        SqlAlchemyUserRepository(session),
        request.app.state.event_log,
        SqlAlchemyDeliveryLog(session),
    )


def dataset_repo_factory(request: Request):
    """A factory the streaming use case can call whenever it needs the database.

    Deliberately NOT `Depends(get_session)`: a request-scoped session would live
    as long as the SSE response, which is as long as the user's tab. Scenario 06
    measures what that costs.
    """

    @asynccontextmanager
    async def factory():
        async with request.app.state.sessionmaker() as session:
            yield SqlAlchemyDatasetRepository(session)

    return factory


def stream_uc(request: Request) -> StreamFeed:
    return StreamFeed(dataset_repo_factory(request), request.app.state.event_log)


def sign_subject(subject_id: str, role: str, org: str) -> str:
    """What the gateway computes and this service re-computes.

    HMAC over the exact fields being asserted, so a client cannot keep a valid
    signature and swap the subject it was issued for. `compare_digest` because
    a naive `==` on a MAC leaks its prefix through timing — a small thing that
    is free to get right and embarrassing to get wrong in a security review.
    """
    message = f"{subject_id}|{role}|{org}".encode()
    return hmac.new(GATEWAY_SECRET.encode(), message, hashlib.sha256).hexdigest()


async def current_subject(
    users: Annotated[SqlAlchemyUserRepository, Depends(users_repo)],
    x_auth_subject: Annotated[str | None, Header()] = None,
    x_auth_role: Annotated[str | None, Header()] = None,
    x_auth_org: Annotated[str | None, Header()] = None,
    x_auth_signature: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
) -> Subject:
    """Construct the Subject, or refuse to.

    Read the ordering carefully: in `gateway` and `signed` mode, `X-User-Id` is
    never consulted at all. Not "checked and rejected" — never read. A header
    the code does not read is a header no reviewer has to reason about, and
    that is a stronger property than any validation you could write.
    """
    if SUBJECT_TRUST in ("gateway", "signed"):
        if not (x_auth_subject and x_auth_role and x_auth_org):
            # Fail closed. The gateway is supposed to have set these; if they
            # are missing, this request did not come through the gateway, and
            # a request that skipped authentication has not passed it.
            raise HTTPException(401, "no verified subject: this service is only reachable via the gateway")
        if SUBJECT_TRUST == "signed":
            expected = sign_subject(x_auth_subject, x_auth_role, x_auth_org)
            if not (x_auth_signature and hmac.compare_digest(expected, x_auth_signature)):
                raise HTTPException(401, "subject headers are not signed by the gateway")
        return Subject(
            id=int(x_auth_subject),
            role=x_auth_role,
            org_id=int(x_auth_org),
            provenance="gateway" if SUBJECT_TRUST == "gateway" else "gateway (signed)",
        )

    # LAB_SUBJECT_TRUST=client. Convenient for poking at the service directly,
    # and exactly the hole scenario 03 walks through. The provenance string is
    # returned in responses so you can see, in the payload, that the server
    # believed a header the client typed.
    if not x_user_id:
        raise HTTPException(401, "send X-User-Id: 1 (Alice), 2 (Bob), 3 (Carol) or 4 (Dan)")
    user = await users.get(int(x_user_id))
    if user is None:
        raise HTTPException(401, "unknown subject")
    return Subject(
        id=user.id,
        role=user.role,
        org_id=user.org_id,
        provenance="client-header (UNVERIFIED)",
    )


SubjectDep = Annotated[Subject, Depends(current_subject)]
