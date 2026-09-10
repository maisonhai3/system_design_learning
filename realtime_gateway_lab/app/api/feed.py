"""The SSE endpoint. One long-lived HTTP response, held open on purpose."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text as sa_text

from app.api import deps
from app.api.deps import SubjectDep
from app.usecases.subscribe import ReadMode, StreamFeed, to_wire

router = APIRouter()


@router.get("/feed", tags=["feed"])
async def feed(
    request: Request,
    subject: SubjectDep,
    uc: Annotated[StreamFeed, Depends(deps.stream_uc)],
    last_event_id: Annotated[str | None, Header()] = None,
    cursor: str | None = Query(
        None,
        description=(
            "Resume point, for clients that cannot set headers. EventSource "
            "sends Last-Event-ID automatically; fetch-based clients must not "
            "forget to."
        ),
    ),
    mode: ReadMode = Query(ReadMode.OWN),
    block_ms: int = Query(5000, description="XREAD BLOCK; 0 here means busy-poll"),
    retry_ms: int = Query(3000, description="what the browser waits before reconnecting"),
    max_frames: int | None = Query(None, description="lab: stop after N events"),
    no_accel_header: bool = Query(
        False,
        description=(
            "chaos: omit X-Accel-Buffering, so a buffering proxy actually "
            "buffers. Scenario 05 measures what that does."
        ),
    ),
    hold_session: bool = Query(
        False,
        description=(
            "chaos: hold a pooled database session for the life of the stream, "
            "the way a request-scoped dependency would. Scenario 06 measures "
            "how few connections it takes to starve every other endpoint."
        ),
    ),
):
    """Server-Sent Events over Redis Streams.

    Three things this endpoint does that the textbook version does not:

    1. **A fresh connection starts at `$`, a resume starts at Last-Event-ID.**
       Defaulting a fresh connection to `0-0` replays the whole retained
       history into every new tab. That looks like a feature right up until the
       stream has a thousand entries and every reconnect ships all of them.

    2. **It disconnects when the client does.** `await request.is_disconnected()`
       is the only way to learn that, because writing to a dead socket may not
       raise for a long time — and a generator nobody is reading is a Postgres
       connection and a Redis blocking read that nobody is reclaiming.

    3. **`X-Accel-Buffering: no`.** A reverse proxy that buffers turns a stream
       into a very slow download. Scenario 05 measures it.
    """
    resume_from = last_event_id or cursor

    async def generate():
        held = None
        if hold_session:
            # The anti-pattern, on purpose and behind a flag. This is what
            # `session: AsyncSession = Depends(get_session)` does to a
            # StreamingResponse: the session is acquired when the request
            # starts and released when the response ENDS, which for SSE is
            # when the user closes the tab. Scenario 06 counts the cost.
            held = request.app.state.sessionmaker()
            await held.__aenter__()
            await held.execute(sa_text("SELECT 1"))  # actually take a connection

        gen = uc(
            subject,
            resume_from,
            mode=mode,
            block_ms=block_ms,
            retry_ms=retry_ms,
            max_frames=max_frames,
        )
        try:
            async for frame in gen:
                if await request.is_disconnected():
                    break
                yield to_wire(frame)
        except asyncio.CancelledError:
            # The client hung up mid-frame. Normal, and not an error — but it
            # has to be caught, or every disconnect is a stack trace and your
            # error rate becomes a measure of how many users closed a tab.
            raise
        finally:
            await gen.aclose()
            if held is not None:
                await held.__aexit__(None, None, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=stream_headers(no_accel_header),
    )


def stream_headers(omit_accel: bool) -> dict[str, str]:
    headers = {
        # Nothing in this path may buffer, cache or transform the body.
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
    }
    if not omit_accel:
        # nginx-specific and worth sending anyway: it is the one header that
        # turns off proxy_buffering per RESPONSE rather than per location, so
        # it protects a stream that is proxied by someone else's config.
        headers["X-Accel-Buffering"] = "no"
    return headers
