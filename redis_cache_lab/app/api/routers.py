"""HTTP layer. BackgroundTasks lives here and nowhere else."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response

from app.adapters.postgres import SqlAlchemyGrantRepository
from app.adapters.redis_cache import RedisCacheGateway
from app.api import deps
from app.usecases import keys
from app.usecases.get_user import GetUser
from app.usecases.list_datasets import ListVisibleDatasets
from app.usecases.update_role import Mode, UpdateRole

router = APIRouter()


@router.get("/users/{user_id}", tags=["read"])
async def read_user(
    user_id: int,
    response: Response,
    uc: Annotated[GetUser, Depends(deps.get_user_uc)],
    versioned: bool = Query(False, description="scenario 03's version-keyed read"),
    ttl: int | None = Query(keys.DEFAULT_TTL, description="null for no expiry — scenario 02"),
    stall_ms: int = Query(
        0,
        description=(
            "chaos: pause between reading Postgres and writing Redis, widening "
            "the scenario 03 window so two curls can hit it by hand"
        ),
    ),
):
    result = await uc(user_id, versioned=versioned, ttl=ttl, stall_ms=stall_ms)
    if result is None:
        raise HTTPException(404, "no such user")
    response.headers["X-Cache"] = "HIT" if result.cache_hit else "MISS"
    response.headers["X-Cache-Key"] = result.key
    return {"user": result.user.__dict__, "cache_hit": result.cache_hit, "key": result.key}


@router.put("/users/{user_id}/role", tags=["write"])
async def update_role(
    user_id: int,
    role: Annotated[str, Query(pattern="^(guest|member|admin)$")],
    background_tasks: BackgroundTasks,
    cache: Annotated[RedisCacheGateway, Depends(deps.get_cache)],
    uc: Annotated[UpdateRole, Depends(deps.update_role_uc)],
    mode: Mode = Query(Mode.DEL_AFTER_COMMIT, description="try them all; two are bugs"),
):
    """The shape worth copying.

    Line by line, and why each line is where it is:

      1. The use case runs. If it raises, execution stops here and the client
         gets a 4xx/5xx — so nothing below can run against a failed write.
         That is what makes "invalidate only after a successful commit" true:
         ordinary control flow, not a transaction manager.

      2. The use case RETURNS what to invalidate; it does not invalidate.
         Domain knowledge (which keys) stays in the domain. Scheduling
         (BackgroundTasks) stays in the layer that has a scheduler. A cronjob
         calling the same use case gets the same list and runs it inline.

      3. add_task, not add. `BackgroundTasks.add_task(fn, *args)` is the API;
         it runs AFTER the response is sent, in this process, in memory —
         which is exactly why scenario 02 exists.
    """
    result = await uc(user_id, role, mode)  # 1
    if result is None:
        raise HTTPException(404, "no such user")

    if result.invalidation.keys:  # 2
        background_tasks.add_task(cache.delete, *result.invalidation.keys)  # 3

    return {
        "user": result.user.__dict__,
        "mode": mode.value,
        "invalidation": {
            "keys": list(result.invalidation.keys),
            "reason": result.invalidation.reason,
            "scheduled": "BackgroundTasks (in-process, in-memory, lost on SIGKILL)",
        },
    }


@router.get("/datasets", tags=["authz"])
async def list_datasets(
    response: Response,
    subject: Annotated[int, Depends(deps.current_subject)],
    uc: Annotated[ListVisibleDatasets, Depends(deps.list_datasets_uc)],
    get_user: Annotated[GetUser, Depends(deps.get_user_uc)],
    leaky_key: bool = Query(
        False, description="scenario 05: drop the subject from the key and watch data leak"
    ),
):
    """ABAC: you see public and internal rows in your org, plus what you were granted."""
    me = await get_user(subject)
    if me is None:
        raise HTTPException(401, "unknown subject")
    result = await uc(subject, me.user.org_id, leaky_key=leaky_key)
    response.headers["X-Cache"] = "HIT" if result.cache_hit else "MISS"
    response.headers["X-Cache-Key"] = result.key
    return {
        "subject": subject,
        "datasets": [d.__dict__ for d in result.datasets],
        "cache_hit": result.cache_hit,
        "key": result.key,
    }


@router.delete("/grants/{user_id}/{dataset_id}", tags=["authz"])
async def revoke_grant(
    user_id: int,
    dataset_id: int,
    background_tasks: BackgroundTasks,
    repo: Annotated[SqlAlchemyGrantRepository, Depends(deps.grants_repo)],
    cache: Annotated[RedisCacheGateway, Depends(deps.get_cache)],
    get_user: Annotated[GetUser, Depends(deps.get_user_uc)],
    invalidate: bool = Query(True, description="set false to watch revocation latency == TTL"),
):
    """The write that has to remember the read's cache.

    This endpoint belongs to the grants team. The cache it must invalidate
    belongs to the catalog team. That gap is where most stale-permission
    incidents come from — which is the argument for a generation counter: the
    grants team bumps one number and does not need to know the catalog's key
    layout at all.
    """
    removed = await repo.revoke(user_id, dataset_id)
    target = await get_user(user_id)
    if target is None:
        raise HTTPException(404, "no such user")

    if invalidate:
        # One INCR invalidates every cached list in the org. O(1), no SCAN.
        background_tasks.add_task(cache.incr, keys.org_generation(target.user.org_id))

    return {
        "revoked": removed,
        "invalidated": "org generation bumped" if invalidate else "nothing — watch the TTL",
        "authz_ttl_seconds": keys.AUTHZ_TTL,
    }
