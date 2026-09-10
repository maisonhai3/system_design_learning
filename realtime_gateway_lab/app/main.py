"""App factory. Owns the connection lifecycle and nothing else."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.postgres import make_engine, make_sessionmaker
from app.adapters.redis_streams import RedisBroadcast, RedisEventLog
from app.api import deps, feed, routers

DESCRIPTION = """
A realtime feed behind a real API gateway.

**Try this, in this order:**

1. `GET /whoami` — look at `provenance`. Through the gateway it says
   `gateway`; straight at the service on :8093 it says `client-header
   (UNVERIFIED)`.
2. `POST /events/11?mode=fanout` as Alice, then open `GET /feed` as Bob.
   The restricted event is not in his stream — it was never written there.
3. `POST /events/11?mode=shared`, then `GET /feed?mode=shared_filtered` as
   Bob. Same outcome, and the event was in his process's memory to get it.
4. Revoke Alice's grant, publish again, and read her feed. Scenario 02.

`./lab.sh sse alice` opens a stream in your terminal; `./lab.sh publish 11`
pushes into it.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = make_engine(deps.DSN)
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.event_log = RedisEventLog(deps.REDIS_URL)
    app.state.broadcast = RedisBroadcast(deps.REDIS_URL)
    yield
    await app.state.event_log.close()
    await app.state.broadcast.close()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Realtime Gateway Lab",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(routers.router)
    app.include_router(feed.router)
    return app


app = create_app()
