# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "redis"]
# ///
"""Scenario 05 — caching an authorization decision, and the two ways it leaks."""

import json
import pathlib
import sys

import redis as redis_lib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import Lab, redis_client, reset  # noqa: E402

ALICE, BOB = 1, 2
ORG = 100
PAYROLL = 11

VISIBLE_SQL = """
SELECT d.id
FROM datasets d
WHERE d.org_id = %s
  AND (d.classification IN ('public', 'internal')
       OR EXISTS (SELECT 1 FROM dataset_grants g
                  WHERE g.dataset_id = d.id AND g.user_id = %s))
ORDER BY d.id
"""

lab = Lab(
    "05 — THE PERMISSION CACHE",
    "The only cache where being wrong is a security incident, not a stale page.",
)
reset()

A = lab.session("as-A")  # a request arriving as Alice
B = lab.session("as-B")  # a request arriving as Bob
ADM = lab.session("admin")
rdb = redis_client()


def visible(s, user_id):
    return [r[0] for r in s.sql(VISIBLE_SQL, (ORG, user_id))]


# ---------------------------------------------------------------------------
lab.section("Anomaly 1: the key that forgot who was asking")
lab.note(
    """
    `GET /datasets` returns the datasets you are allowed to see. It is an
    expensive query — a join against grants, filtered by classification — so
    somebody caches it, keyed by the thing the endpoint is obviously about:
    the organisation.

    Alice has a grant on the restricted payroll dataset. Bob does not.
    """
)

leaky_key = f"catalog:v1:datasets:org:{ORG}"

alice_sees = visible(A, ALICE)
A.cache_set(leaky_key, json.dumps(alice_sees), ttl=300, note="keyed by org only")

bob_gets = json.loads(B.cache_get(leaky_key))
B.say("cache HIT — Bob's request never runs the grant join at all")

lab.broke(
    PAYROLL in bob_gets and PAYROLL not in visible(B, BOB),
    f"Bob is served dataset {PAYROLL} (restricted payroll), which he has no grant for",
    """
    Bob's request did not fail an authorization check. It never performed one.
    The check had been done — for Alice — and its result was filed under a name
    that did not mention her.

    This is not a Redis bug and not really a caching bug. It is a memoization
    bug: the cache key must be a function of EVERY input the value depends on.
    The value depended on the subject. The key did not mention the subject.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Fix: the key is the whole input to the decision")

reset()
scoped = lambda uid: f"catalog:v1:datasets:org:{ORG}:subject:{uid}"

A.cache_set(scoped(ALICE), json.dumps(visible(A, ALICE)), ttl=300)
B.cache_set(scoped(BOB), json.dumps(visible(B, BOB)), ttl=300)

alice_cached = json.loads(A.cache_get(scoped(ALICE)))
bob_cached = json.loads(B.cache_get(scoped(BOB)))

lab.held(
    PAYROLL in alice_cached and PAYROLL not in bob_cached,
    f"Alice sees {alice_cached}, Bob sees {bob_cached}",
    """
    The test to apply to any authorization cache key, in one question:

        If two requests would produce different answers, can they produce the
        same key?

    If yes, you have a leak — not a risk of one. Everything the decision reads
    must appear in the key: subject, tenant, role, and any request attribute
    the policy consults (an `as_of` date, an impersonation header, a scope from
    the token). Anything you leave out, you are asserting is irrelevant.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Anomaly 2: revocation latency IS your TTL")
lab.note(
    """
    Security asks: "we revoked Alice's payroll access at 14:02, when did it
    take effect?" The honest answer is a property of your cache, not of your
    authorization code.
    """
)

ADM.sql(
    "DELETE FROM dataset_grants WHERE user_id = %s AND dataset_id = %s",
    (ALICE, PAYROLL),
    note="revoked in Postgres, 14:02:00",
)

still = json.loads(A.cache_get(scoped(ALICE)))
lab.broke(
    PAYROLL in still,
    f"Alice's next request still returns {PAYROLL} — the revoke has not taken effect",
    """
    With a 300s TTL and no invalidation, "revoked at 14:02" means "loses access
    at some point before 14:07". Write that sentence into the incident review
    and see whether anyone is happy with it.

    The number is not a caching detail. It is the answer to "how long can a
    fired employee still read payroll", and somebody outside engineering has an
    opinion about it.
    """,
)

# The write path must invalidate the subject's cached decisions.
ADM.cache_del(scoped(ALICE), note="revoke invalidates the subject's entries")
after = visible(A, ALICE)
lab.held(
    PAYROLL not in after,
    "with invalidation on the revoke path, access ends when the transaction commits",
    """
    Note WHERE that DEL has to live: in the grant-revocation use case, not in
    the dataset-listing one. The team that owns the write is the team that owns
    the invalidation, and they usually are not the team that added the cache.
    That gap is where most stale-permission incidents actually come from.
    """,
)

lab.note(
    """
    Two asymmetries worth stating explicitly, because they justify different
    numbers for cases that look symmetric:

      A stale ALLOW  is a security incident.  Invalidate it synchronously,
                     give it the shortest TTL you can afford.
      A stale DENY   is a support ticket.     A longer TTL is defensible.

    And the fan-out problem: revoking a grant on a ROLE, not a user, changes
    the answer for every user holding that role. You cannot enumerate their
    keys, and `KEYS pattern` is not the answer (scenario 07). That is what
    generation counters are for.
    """
)


# ---------------------------------------------------------------------------
lab.section("Cache the inputs, not the decision")
lab.note(
    """
    The instinct is to cache the answer: `can_read(user, dataset) -> bool`.
    Count the keys before you commit to it.
    """
)

users, datasets = 10_000, 100_000
lab.measure("cache the DECISION, one key per (user, resource):", f"{users * datasets:,} keys")
lab.measure("cache the INPUT, one grant-set per user:", f"{users:,} keys")
lab.measure("keys to invalidate when one user's grants change:", "1,000,000  vs  1")

lab.note(
    """
    Same information, two designs, six orders of magnitude apart. The grant set
    is small, changes rarely, is invalidated by exactly one write, and the
    policy evaluation on top of it is microseconds of pure Python.

    So: cache the INPUTS to the decision and evaluate the policy every time.
    Then the policy is never stale, and the only thing that can go out of date
    is the small set of facts you can name and invalidate precisely.

    The exception that proves it: an expensive decision over inputs that
    genuinely never change — a static role→permission matrix — is fine to
    cache as a decision, because "invalidate on change" is a deploy.
    """
)


# ---------------------------------------------------------------------------
lab.section("When Redis is down, which way does the guard fail?")

reset()
dead = redis_lib.Redis.from_url("redis://localhost:6399/0", socket_connect_timeout=1)


def guard_fail_open(user_id, dataset_id):
    """The bug. Written by someone protecting their p99, not their data."""
    try:
        cached = dead.get(f"authz:v1:subject:{user_id}:dataset:{dataset_id}")
        return cached == "allow"
    except redis_lib.RedisError:
        return True  # "cache is down, don't block users" — this is a backdoor


def guard_correct(s, user_id, dataset_id):
    """Redis is an optimisation. Postgres is the authority."""
    try:
        cached = dead.get(f"authz:v1:subject:{user_id}:dataset:{dataset_id}")
        if cached is not None:
            return cached == "allow"
    except redis_lib.RedisError:
        pass  # degrade to the source of truth; do NOT degrade the decision
    return dataset_id in visible(s, user_id)


leaked = guard_fail_open(BOB, PAYROLL)
lab.broke(
    leaked is True,
    "with Redis down, the fail-open guard grants Bob access to payroll",
    """
    A cache outage has become a global authorization bypass, and the only
    signal is that your latency looks great. Someone wrote that `except` clause
    to protect availability during an incident. It works: everything stays up,
    and everyone can read everything.
    """,
)

B.say("same request, through the guard that treats Redis as an optimisation")
denied = guard_correct(B, BOB, PAYROLL)
lab.held(
    denied is False,
    "the correct guard falls through to Postgres and denies",
    """
    The posture, in one line each:

      Redis unreachable     → fall through to the database. Slower, still correct.
      Database unreachable  → DENY. An authorization check that cannot be made
                              has not been passed.
      Cache says allow      → still only as trustworthy as your revoke path.

    "Fail open" is occasionally the right call — for a feature flag, a rate
    limiter, a recommendation. It is never the right call for the check that
    decides who reads payroll.
    """,
)
dead.close()

lab.takeaway(
    """
    "An authorization cache is different from a data cache in three ways.
     First, the key has to contain every input the decision depends on —
     subject, tenant, and any request attribute the policy reads — because if
     two requests with different answers can produce the same key, that is a
     data leak, not a risk of one. Second, the TTL is the revocation latency:
     'revoked at 14:02' really means 'loses access before 14:07' unless the
     revoke path invalidates, so I invalidate on the write and keep the TTL
     short as a backstop. Third, I cache the INPUTS — the user's grant set —
     rather than per-resource decisions, because that is one key to invalidate
     instead of a million, and the policy itself is then never stale. And the
     guard fails closed: Redis down means fall through to Postgres, database
     down means deny."
    """
)

rdb.close()
lab.finish()
