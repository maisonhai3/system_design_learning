"""The write path: getting one event into the right mailboxes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from app.domain.entities import Dataset, Subject
from app.domain.ports import DatasetRepository, EventLog, UserRepository
from app.usecases import authorize, keys


class FanoutMode(str, Enum):
    """Three designs, and only one of them is right for a permissioned feed."""

    SHARED = "shared"  # one stream, everyone listens, filter on read
    FANOUT = "fanout"  # a copy of the body in each authorised mailbox
    POINTER = "pointer"  # the body once, an id in each mailbox — see scenario 02


@dataclass
class PublishResult:
    event_id: str
    mode: FanoutMode
    delivered_to: list[int]
    withheld_from: list[int]
    reasons: dict[int, str]
    bytes_written: int


class PublishDatasetEvent:
    def __init__(
        self,
        datasets: DatasetRepository,
        users: UserRepository,
        log: EventLog,
        deliveries=None,
    ):
        self.datasets = datasets
        self.users = users
        self.log = log
        self.deliveries = deliveries

    async def __call__(self, dataset_id: int, kind: str, mode: FanoutMode) -> PublishResult | None:
        dataset = await self.datasets.get(dataset_id)
        if dataset is None:
            return None

        fields = _fields(dataset, kind)
        payload_bytes = sum(len(k) + len(v) for k, v in fields.items())

        if mode is FanoutMode.SHARED:
            # No authorization happens here at all. The event is written once,
            # to a stream every subscriber reads, and the decision about who
            # may see it is deferred to N readers who must each get it right.
            event_id = await self.log.append(keys.GLOBAL_STREAM, fields, keys.MAXLEN)
            return PublishResult(event_id, mode, [], [], {}, payload_bytes)

        # Fan-out runs the policy BACKWARDS: not "may this subject see it" but
        # "which subjects may". That is a different query, and a repository that
        # cannot answer it rules this design out entirely.
        allowed = set(await self.datasets.subjects_allowed_to_see(dataset_id))
        everyone = await self.users.all_ids()
        reasons = await self._reasons(dataset, everyone)

        if mode is FanoutMode.POINTER:
            # The body once, under a key. Each mailbox gets a reference.
            event_id = await self.log.append(keys.GLOBAL_STREAM, fields, keys.MAXLEN)
            await self.log.put_body(keys.body(event_id), json.dumps(fields))
            written = payload_bytes
            for uid in sorted(allowed):
                await self.log.append(
                    keys.user_stream(uid), {"ref": event_id}, keys.MAXLEN
                )
                written += len("ref") + len(event_id)
        else:
            # A full copy per mailbox. Simple, fast to read, and the reason
            # storage grows with (event size x audience size).
            event_id = ""
            written = 0
            for uid in sorted(allowed):
                event_id = await self.log.append(keys.user_stream(uid), fields, keys.MAXLEN)
                written += payload_bytes

        await self._record(event_id, dataset_id, allowed, reasons)
        return PublishResult(
            event_id=event_id,
            mode=mode,
            delivered_to=sorted(allowed),
            withheld_from=sorted(set(everyone) - allowed),
            reasons=reasons,
            bytes_written=written,
        )

    async def _reasons(self, dataset: Dataset, user_ids: list[int]) -> dict[int, str]:
        """Why each user was or was not delivered to.

        Honest note: this is an N+1, two queries per user, and it is kept that
        way because it is the audit path and it reads like the policy. The
        DECISION path is not — `subjects_allowed_to_see` answers "who may see
        this" in one query, which is what makes fan-out O(1) queries rather
        than O(users). If you ever move this loop onto the hot path, that is
        the moment to collapse it.
        """
        out: dict[int, str] = {}
        for uid in user_ids:
            user = await self.users.get(uid)
            if user is None:
                continue
            granted = frozenset(
                d.id for d in await self.datasets.visible_to(uid, user.org_id)
            )
            subject = Subject(user.id, user.role, user.org_id, provenance="fanout-worker")
            out[uid] = authorize.why(subject, dataset, granted)
        return out

    async def _record(self, event_id, dataset_id, allowed, reasons) -> None:
        if self.deliveries is None or not event_id:
            return
        await self.deliveries.record_many(
            [
                {"e": event_id, "u": uid, "d": dataset_id, "r": reason}
                for uid, reason in reasons.items()
                if uid in allowed
            ]
        )


def _fields(dataset: Dataset, kind: str) -> dict[str, str]:
    # Redis stream fields are flat string→string. Keeping the event flat rather
    # than stuffing a JSON blob into one field means you can XADD, read it in
    # redis-cli, and see what it is — which matters at 3am.
    return {
        "kind": kind,
        "dataset_id": str(dataset.id),
        "org_id": str(dataset.org_id),
        "classification": dataset.classification,
        "dataset_name": dataset.name,
    }
