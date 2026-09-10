# The permission cache — the only cache where being wrong is a security incident

**The claim you are practising:** *"An authorization cache differs from a data
cache in three ways: the key must contain every input to the decision, the TTL
*is* the revocation latency, and I cache the inputs rather than the decisions."*

This is the scenario closest to the job description: RBAC by role, ABAC by data
attribute, enforcement at the gateway *and* in the service.

## Anomaly 1: the key that forgot who was asking

`GET /datasets` returns the datasets you may see. It is an expensive query — a
join against grants, filtered by classification — so somebody caches it, keyed by
the thing the endpoint is obviously about:

```python
key = f"catalog:v1:datasets:org:{org_id}"      # the org. Not the user.
```

Alice has a grant on the restricted payroll dataset. Bob does not. Alice's
request populates the cache. Bob's request **hits** it.

Bob did not fail an authorization check. **He never performed one.** The check
had been made — for Alice — and filed under a name that did not mention her.

This is not a Redis bug or really a caching bug. It is a memoization bug:

> **The cache key must be a function of every input the value depends on.**

The value depended on the subject. The key did not mention the subject.

### The test to apply to any authorization cache key

> *If two requests would produce different answers, can they produce the same key?*

If yes, you have a leak — not a risk of one, a leak. Everything the policy reads
must appear in the key: subject, tenant, role, and any request attribute the
policy consults (an `as_of` date, an impersonation header, a token scope).
**Anything you leave out, you are asserting is irrelevant to the answer.**

## Anomaly 2: revocation latency *is* your TTL

> *"We revoked Alice's payroll access at 14:02. When did it take effect?"*

With a 300s TTL and no invalidation on the revoke path, the honest answer is
*"some time before 14:07"*. That sentence is not a caching detail. It is the
answer to "how long can a fired employee still read payroll", and someone outside
engineering has an opinion about it.

The fix is invalidation on the write path — and notice **where** it has to live:
in the grant-revocation use case, not in the dataset-listing one. The team that
owns the write owns the invalidation, and they are usually not the team that
added the cache. That gap is where most stale-permission incidents actually come
from.

### Two asymmetries

|  | Failure mode | Posture |
|---|---|---|
| Stale **allow** | security incident | invalidate synchronously, shortest TTL you can afford |
| Stale **deny** | support ticket | a longer TTL is defensible |

### The fan-out problem

Revoking a permission on a **role** rather than a user changes the answer for
every user holding that role. You cannot enumerate their keys, and
`KEYS pattern` is not the answer — see scenario 07 for what it does to a
single-threaded server, and for the generation-counter pattern that solves this
properly.

## Cache the inputs, not the decision

The instinct is to cache the answer: `can_read(user, dataset) -> bool`. Count the
keys first.

| Design | Keys | Invalidate when one user's grants change |
|---|---:|---:|
| cache the **decision**, per (user, resource) | 1,000,000,000 | 1,000,000 |
| cache the **input**, one grant-set per user | 10,000 | 1 |

At 10k users and 100k datasets. Same information, six orders of magnitude apart.

The grant set is small, changes rarely, is invalidated by exactly one write, and
evaluating the policy on top of it is microseconds of pure Python. So cache the
inputs and evaluate the policy on every request: **the policy is then never
stale, and the only thing that can go out of date is a small set of facts you can
name and invalidate precisely.**

The exception that proves the rule: an expensive decision over inputs that
genuinely never change at runtime — a static role→permission matrix — is fine to
cache as a decision, because "invalidate on change" is a deploy.

## When Redis is down, which way does the guard fail?

```python
try:
    return await redis.get(key) == "allow"
except RedisError:
    return True          # "cache is down, don't block users"
```

Someone wrote that `except` to protect availability during an incident. It works:
everything stays up, and everyone can read everything. **A cache outage has
become a global authorization bypass, and the only signal is that your latency
looks great.**

The correct posture:

| Situation | Behaviour |
|---|---|
| Redis unreachable | fall through to the database — slower, still correct |
| Database unreachable | **deny** — a check that could not be made has not been passed |
| Cache says allow | only as trustworthy as your revoke path |

"Fail open" is occasionally right — a feature flag, a rate limiter, a
recommendation ranker. It is never right for the check that decides who reads
payroll.

## Where this meets the gateway

The job description asks for enforcement at the gateway *and* in the service.
That is defence in depth, and it changes what you are allowed to cache where:

- The **gateway** may cache coarse RBAC — "does this token carry a role that can
  reach this route at all". Cheap, low-cardinality, and wrong only in ways that
  the service will catch.
- The **service** must do the ABAC row filtering itself, against the subject in
  the request. It cannot delegate that to the gateway, because the gateway does
  not know which rows exist.
- Therefore the gateway's cache must never be the *only* check, and the service
  must never trust a header saying "already authorized" that it did not sign.
  Trust boundary: the gateway can *add* claims; it can never *remove* the
  service's obligation to filter.

## Run it

```bash
./lab.sh run 05
```

## The interview answer

> *"You're caching permission checks. What could go wrong?"*

"Three things, in order of how badly they end.

First, the key has to contain every input the decision depends on — subject,
tenant, and any request attribute the policy reads. If two requests with
different answers can produce the same key, that's a data leak, not a risk of
one; I've seen a list endpoint keyed by org rather than by user hand one user
another user's rows.

Second, the TTL is the revocation latency. 'Revoked at 14:02' really means 'loses
access some time before 14:07' unless the revoke path invalidates, so I put the
DEL in the revocation use case and keep the TTL short as a backstop — and I treat
a stale allow and a stale deny as different severities.

Third, I cache the inputs — the user's grant set — not per-resource decisions.
That's one key to invalidate instead of a million, and the policy itself is then
never stale.

And the guard fails closed: Redis down means fall through to Postgres, database
down means deny."
