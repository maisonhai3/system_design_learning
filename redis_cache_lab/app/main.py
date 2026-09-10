"""App factory. Owns the connection lifecycle and nothing else."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.postgres import make_engine, make_sessionmaker
from app.adapters.redis_cache import RedisCacheGateway
from app.api import debug, deps, routers

DESCRIPTION = """
A cache you can break on purpose.

**Try this, in this order:**

1. `GET /users/1` twice — the second is `X-Cache: HIT`.
2. `PUT /users/1/role?role=admin&mode=del_before_commit`, then `GET /users/1`.
   Scenario 01.
3. `GET /debug/keys` and look at `ttl: -1` after
   `GET /users/1?ttl=` — scenario 02.
4. `GET /datasets` as `X-User-Id: 1`, then as `X-User-Id: 2`, both with
   `?leaky_key=true`. Bob sees Alice's payroll. Scenario 05.
5. `DELETE /grants/1/11?invalidate=false`, then `GET /datasets` as user 1.
   Revocation latency is your TTL.

The race in scenario 03 needs two terminals — `./lab.sh demo` drives it.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = make_engine(deps.DSN)
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.cache = RedisCacheGateway(deps.REDIS_URL)
    yield
    await app.state.cache.close()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Redis Cache Lab",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(routers.router)
    app.include_router(debug.router)
    return app


app = create_app()
