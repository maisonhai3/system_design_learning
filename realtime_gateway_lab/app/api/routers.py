"""REST endpoints. The parts that are not a stream."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.adapters.postgres import SqlAlchemyDatasetRepository, SqlAlchemyGrantRepository
from app.adapters.redis_streams import RedisEventLog
from app.api import deps
from app.api.deps import SubjectDep
from app.usecases import keys
from app.usecases.publish import FanoutMode, PublishDatasetEvent

router = APIRouter()


@router.get("/healthz", tags=["ops"])
async def healthz():
    """Unauthenticated on purpose, and therefore says nothing.

    A health endpoint behind auth cannot be probed by the thing that needs to
    probe it. A health endpoint that reports version, database status or queue
    depth to anyone who asks is reconnaissance. "ok" is the whole contract.
    """
    return {"ok": True}


@router.get("/whoami", tags=["trust boundary"])
async def whoami(subject: SubjectDep):
    """Who does this service think you are, and why does it think so?

    `provenance` is the field to watch. "client-header (UNVERIFIED)" means the
    server believed something you typed.
    """
    return {
        "subject": subject.id,
        "role": subject.role,
        "org_id": subject.org_id,
        "provenance": subject.provenance,
        "trust_mode": deps.SUBJECT_TRUST,
    }


@router.get("/datasets", tags=["authz"])
async def list_datasets(
    subject: SubjectDep,
    datasets: Annotated[SqlAlchemyDatasetRepository, Depends(deps.datasets_repo)],
):
    """The ABAC read the gateway could never have done for you."""
    rows = await datasets.visible_to(subject.id, subject.org_id)
    return {"subject": subject.id, "datasets": [d.__dict__ for d in rows]}


@router.post("/events/{dataset_id}", tags=["feed"])
async def publish_event(
    dataset_id: int,
    subject: SubjectDep,
    uc: Annotated[PublishDatasetEvent, Depends(deps.publish_uc)],
    kind: str = Query("dataset.processed"),
    mode: FanoutMode = Query(FanoutMode.FANOUT),
):
    """Publish one event. `mode` picks the fan-out design — see scenario 02."""
    if subject.role != "admin":
        # Coarse RBAC, enforced here as well as at the gateway. Deliberately
        # duplicated: the gateway's copy is an optimisation, this one is the
        # guarantee.
        raise HTTPException(403, "publishing requires the admin role")
    result = await uc(dataset_id, kind, mode)
    if result is None:
        raise HTTPException(404, "no such dataset")
    return {
        "event_id": result.event_id,
        "mode": result.mode.value,
        "delivered_to": result.delivered_to,
        "withheld_from": result.withheld_from,
        "reasons": result.reasons,
        "bytes_written": result.bytes_written,
    }


@router.post("/grants/{user_id}/{dataset_id}", tags=["authz"])
async def grant(
    user_id: int,
    dataset_id: int,
    repo: Annotated[SqlAlchemyGrantRepository, Depends(deps.grants_repo)],
):
    await repo.grant(user_id, dataset_id)
    return {
        "granted": True,
        "note": (
            "Events published BEFORE this grant are not in this user's stream and "
            "never will be. Fan-out froze the decision at write time — scenario 02."
        ),
    }


@router.delete("/grants/{user_id}/{dataset_id}", tags=["authz"])
async def revoke(
    user_id: int,
    dataset_id: int,
    repo: Annotated[SqlAlchemyGrantRepository, Depends(deps.grants_repo)],
):
    removed = await repo.revoke(user_id, dataset_id)
    return {
        "revoked": removed,
        "note": (
            "Events already fanned out to this user's stream are still there. "
            "Revoking a grant does not un-deliver a mailbox — scenario 02."
        ),
    }


@router.get("/debug/headers", tags=["debug"])
async def debug_headers(request: Request):
    """Every X-* header the service actually received.

    The most useful five lines in this lab. A gateway is a machine for adding
    and removing headers, and until you can see what arrived you are guessing
    about which of your middlewares is load-bearing. Send yourself a forged
    `X-Auth-Subject` and watch it not be here.
    """
    return {
        "x_headers": {
            k: v for k, v in request.headers.items() if k.lower().startswith("x-")
        },
        "trust_mode": deps.SUBJECT_TRUST,
    }


@router.get("/debug/streams", tags=["debug"])
async def debug_streams(log: Annotated[RedisEventLog, Depends(deps.get_log)]):
    """Every stream and what is in it. A lab affordance; do not ship it."""
    out = {}
    for name in await log.streams():
        out[name] = [
            {"id": eid, **fields} for eid, fields in await log.range(name)
        ]
    return {"maxlen_per_stream": keys.MAXLEN, "streams": out}
