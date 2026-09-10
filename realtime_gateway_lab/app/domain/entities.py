"""Domain entities. Plain data, no framework, no I/O.

The test for whether this layer stayed honest: `python -c "import
app.domain.entities"` must work with Postgres down, Redis down, and FastAPI
uninstalled.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str
    role: str
    org_id: int


@dataclass(frozen=True)
class Dataset:
    id: int
    org_id: int
    name: str
    classification: str


@dataclass(frozen=True)
class Subject:
    """Who is making this request, as the SERVICE understands it.

    Note what is not in here: a token, a header, a signature. By the time a use
    case sees a Subject, authentication is over and settled. The whole argument
    of scenario 03 is about who is allowed to construct one of these.
    """

    id: int
    role: str
    org_id: int
    #  How we came to believe this. Carried because scenario 03 is about the
    #  difference between "the gateway verified a signature" and "the client
    #  typed a header", and a system that cannot tell them apart is one typo
    #  away from an incident.
    provenance: str


@dataclass(frozen=True)
class FeedEvent:
    """Something happened to a dataset. The unit this lab pushes over SSE."""

    dataset_id: int
    org_id: int
    classification: str
    dataset_name: str
    kind: str  # 'dataset.processed', 'dataset.failed', ...
