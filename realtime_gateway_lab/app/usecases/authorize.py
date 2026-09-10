"""The ABAC policy. One pure function, deliberately.

It is pure — no I/O, no clock, no framework — so it can be called in three
different places that could otherwise drift apart:

  * on the READ path, filtering a shared stream (scenario 02's naive mode);
  * on the WRITE path, deciding whose mailbox an event belongs in (fan-out);
  * on the DEREFERENCE path, re-checking when a pointer is exchanged for a body.

A policy that exists twice is a policy that will be enforced once. If you take
one habit from this file, take that one: the rule gets a name and a home, and
every enforcement point calls it. What differs between enforcement points is
WHEN it runs and WHAT it costs, never WHAT IT SAYS.
"""

from __future__ import annotations

from app.domain.entities import Dataset, Subject

OPEN_CLASSIFICATIONS = ("public", "internal")


def may_see(subject: Subject, dataset: Dataset, granted_dataset_ids: frozenset[int]) -> bool:
    """The whole policy, and the same rule as redis_cache_lab's scenario 05.

    Read it as three ANDed facts, because that is how you will have to defend
    it: the row is in your organisation, and either it is not restricted, or
    you hold an explicit grant on it.
    """
    if dataset.org_id != subject.org_id:
        return False
    if dataset.classification in OPEN_CLASSIFICATIONS:
        # A guest sees only public rows. This is the one place RBAC (the role)
        # and ABAC (the row's classification) actually meet, and it is worth
        # noticing how small the RBAC part is once real data is involved.
        if subject.role == "guest" and dataset.classification != "public":
            return False
        return True
    return dataset.id in granted_dataset_ids


def why(subject: Subject, dataset: Dataset, granted: frozenset[int]) -> str:
    """A human-readable reason, recorded in feed_deliveries.

    Logging the REASON rather than the verdict is what makes an authorization
    system debuggable a month later. "denied" tells you nothing; "restricted,
    no grant" tells you which of three clauses fired.
    """
    if dataset.org_id != subject.org_id:
        return "denied: different org"
    if dataset.classification in OPEN_CLASSIFICATIONS:
        if subject.role == "guest" and dataset.classification != "public":
            return "denied: guest, non-public"
        return f"allowed: {dataset.classification} in own org"
    if dataset.id in granted:
        return "allowed: explicit grant"
    return "denied: restricted, no grant"
