"""The read path: one long-lived SSE connection, and how it resumes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator

from app.domain.entities import Dataset, Subject
from app.domain.ports import DatasetRepositoryFactory, EventLog
from app.usecases import authorize, keys


class ReadMode(str, Enum):
    OWN = "own"  # read my mailbox — fan-out already decided
    SHARED_FILTERED = "shared_filtered"  # read the global stream, filter here
    POINTER = "pointer"  # read ids from my mailbox, re-authorize the body


# The SSE cursor sentinels, and the difference that bites people:
#   "$"    only entries added AFTER I connect
#   "0-0"  every entry the stream still holds
FROM_NOW = "$"
FROM_START = "0-0"


@dataclass
class Frame:
    """One SSE frame, still in domain terms. The router does the wire format."""

    event_id: str | None
    event: str
    data: str
    comment: bool = False


class StreamFeed:
    """Yields SSE frames for one subject until the client goes away.

    The generator owns three things the naive version forgets:

      1. a cursor that starts at "$" for a fresh connection and at the client's
         Last-Event-ID for a resume — those are different requests and must not
         share a default;
      2. a keep-alive comment whenever the block times out, because an idle
         connection is an invisible connection to every proxy in the path;
      3. a `retry:` hint, so the reconnect interval is the SERVER's decision.
    """

    def __init__(self, datasets: DatasetRepositoryFactory, log: EventLog):
        # A factory, not a repository. This connection may live for hours; a
        # pooled database session must not. See DatasetRepositoryFactory.
        self.datasets = datasets
        self.log = log

    async def __call__(
        self,
        subject: Subject,
        last_event_id: str | None,
        *,
        mode: ReadMode = ReadMode.OWN,
        block_ms: int = 5_000,
        retry_ms: int = 3_000,
        max_frames: int | None = None,
    ) -> AsyncIterator[Frame]:
        stream = (
            keys.GLOBAL_STREAM if mode is ReadMode.SHARED_FILTERED
            else keys.user_stream(subject.id)
        )

        # A fresh connection starts at "$". A resume starts where the client
        # says it stopped. Defaulting a fresh connection to "0-0" replays the
        # entire retained history to every new tab — a bug that looks like a
        # feature until the stream has a thousand entries in it.
        cursor = last_event_id or FROM_NOW
        resumed = last_event_id is not None

        yield Frame(None, "retry", str(retry_ms), comment=False)
        yield Frame(
            None,
            "hello",
            json.dumps(
                {
                    "subject": subject.id,
                    "provenance": subject.provenance,
                    "mode": mode.value,
                    "cursor": cursor,
                    "resumed": resumed,
                }
            ),
        )

        sent = 0
        while max_frames is None or sent < max_frames:
            entries = await self.log.read(stream, cursor, block_ms=block_ms, count=100)
            if not entries:
                # The block expired with nothing to say. Say nothing, out loud:
                # a comment frame keeps the socket warm without inventing an
                # event the client would have to ignore.
                yield Frame(None, "", ": keep-alive", comment=True)
                continue

            for event_id, fields in entries:
                cursor = event_id
                frame = await self._render(subject, event_id, fields, mode)
                if frame is None:
                    continue
                yield frame
                sent += 1
                if max_frames is not None and sent >= max_frames:
                    return

    async def _render(self, subject, event_id, fields, mode) -> Frame | None:
        if mode is ReadMode.OWN:
            # Nothing to decide: fan-out already decided, at write time. That
            # is the speed, and scenario 02 is about what it costs.
            return Frame(event_id, fields.get("kind", "message"), json.dumps(fields))

        if mode is ReadMode.POINTER:
            raw = await self.log.get_body(keys.body(fields["ref"]))
            if raw is None:
                # The body was trimmed or expired out from under the pointer.
                # Dropping it silently would be a lie; telling the client is
                # how a resume window becomes visible instead of mysterious.
                return Frame(event_id, "gap", json.dumps({"ref": fields["ref"]}))
            fields = json.loads(raw)

        # SHARED_FILTERED and POINTER both re-run the policy here, NOW, against
        # the grants as they currently stand. That is the property fan-out
        # trades away, and the reason this mode still exists.
        #
        # The session is opened here and released three lines later, rather than
        # held for the life of the stream. That is the whole difference between
        # a service that supports 10,000 concurrent feeds and one that stops at
        # the size of its connection pool.
        async with self.datasets() as datasets:
            dataset = await datasets.get(int(fields["dataset_id"]))
            if dataset is None:
                return None
            granted = frozenset(
                d.id for d in await datasets.visible_to(subject.id, subject.org_id)
            )
        if not authorize.may_see(subject, dataset, granted):
            return None
        return Frame(event_id, fields.get("kind", "message"), json.dumps(fields))


def to_wire(frame: Frame) -> str:
    """Domain Frame → the SSE wire format.

    The format is four optional fields and a blank line, and the blank line is
    the part people get wrong: a frame is not dispatched until the parser sees
    "\\n\\n". A generator that forgets the second newline produces a stream that
    connects, transfers bytes, and never fires a single onmessage.
    """
    if frame.comment:
        return f"{frame.data}\n\n"
    if frame.event == "retry":
        return f"retry: {frame.data}\n\n"
    out = ""
    if frame.event_id:
        # The `id:` line is what the browser stores and replays as
        # Last-Event-ID. No id line, no resume — the rest of the design is
        # decoration without it.
        out += f"id: {frame.event_id}\n"
    out += f"event: {frame.event}\n"
    for line in frame.data.splitlines() or [""]:
        out += f"data: {line}\n"
    return out + "\n"
