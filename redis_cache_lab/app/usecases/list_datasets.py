"""The ABAC read: which datasets may this subject see, and how it gets cached."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.domain.entities import Dataset
from app.domain.ports import CacheGateway, DatasetRepository
from app.usecases import keys


@dataclass
class ListResult:
    datasets: list[Dataset]
    cache_hit: bool
    key: str


class ListVisibleDatasets:
    def __init__(self, datasets: DatasetRepository, cache: CacheGateway):
        self.datasets = datasets
        self.cache = cache

    async def __call__(self, subject_id: int, org_id: int, *, leaky_key: bool = False):
        # The generation counter makes "invalidate every list in this org" one
        # INCR instead of a SCAN. See scenario 07.
        gen = await self.cache.get(keys.org_generation(org_id))
        generation = int(gen) if gen is not None else 1

        build = keys.visible_datasets_LEAKY if leaky_key else keys.visible_datasets
        key = build(org_id, generation, subject_id)

        cached = await self.cache.get(key)
        if cached is not None:
            return ListResult([Dataset(**d) for d in json.loads(cached)], True, key)

        rows = await self.datasets.visible_to(subject_id, org_id)
        await self.cache.set(key, json.dumps([d.__dict__ for d in rows]), keys.AUTHZ_TTL)
        return ListResult(rows, False, key)
