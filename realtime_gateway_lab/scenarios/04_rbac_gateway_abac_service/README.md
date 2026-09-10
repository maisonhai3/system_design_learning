# RBAC at the gateway, ABAC at the service

**The claim you are practising:** *"The gateway decides route-level RBAC from
the token and never touches a database. It cannot decide row-level ABAC, because
that needs the grants table — and giving the gateway a connection to it would
create one component that can read every tenant's data and sits on every
request."*

## What the gateway CAN decide

```
POST /events/11  as Bob  →  403  "POST /events* requires ['admin']"
```

This request never reached the API service. The case for coarse RBAC at the edge
is a good one:

- **cheap** — one signature check and a table lookup, no database;
- **fails fast** — a request that cannot possibly succeed is not carried through
  your service mesh to find out;
- **central** — the token format, the clock-skew allowance and the denylist exist
  once, not once per service.

Note what is being decided: *may a token with this **role** reach this **method**
and **path***. Nothing about rows, nothing about ownership, nothing requiring
knowledge of what exists.

## What it CANNOT decide

Four requests, identical gateway verdict, four different result sets:

```
GET /datasets
  alice (admin, org 100) → [10, 11, 12]     11 = restricted, explicit grant
  bob   (member, org 100)→ [10, 12]
  dan   (guest,  org 100)→ [12]             guests get public only
  carol (admin,  org 200)→ [13, 14]         different tenant entirely
```

To produce any one of those you must join grants against classifications — you
must know what rows exist and who was granted what. The gateway has never seen
that table, and **giving it a connection so it could would make it an upstream
service with a gateway's blast radius.**

So the division is not a compromise. It is the only correct one:

| | decided from | where | cost |
|---|---|---|---|
| RBAC, coarse | the token | the edge | a dict lookup |
| ABAC, row-level | the data | the service | a query, in the `WHERE` clause |

"In the `WHERE` clause" is load-bearing. Filtering after fetching means the
restricted rows were on the wire, in memory, and one `logger.debug` from your log
aggregator.

## The policy exists twice. A test is what keeps it one policy.

The row filter is written in SQL (so restricted rows are never fetched) and the
same rule in Python (so the fan-out worker and the pointer dereference can
evaluate it without a query). Two implementations of one policy is drift waiting
to happen — **and it happened while this lab was being written**: the Python
version excluded guests from internal rows and the SQL version did not, so Dan
could list a dataset the policy said he could not see.

Nobody decides to have two policies. Somebody adds a clause to the one they have
open. The defence is not discipline:

```
(subject, dataset) pairs checked: 20
✓ the SQL policy and the Python policy agree on every pair
```

A cross product of the seed data, running in CI, failing on the pull request that
introduces the drift instead of in the incident three months later. **If you can
only have one authorization test, have this one.**

## Why the service repeats the check the gateway already made

Go around the gateway with a perfectly valid, correctly *signed* member
assertion:

```
POST /events/11  →  403  "publishing requires the admin role"
```

The two checks are not duplicates, they are different claims:

- the **gateway's** is an *optimisation* — reject early, cheaply, centrally;
- the **service's** is the *guarantee* — it holds no matter how the request
  arrived.

Delete the gateway's and you get slower. Delete the service's and you get a
system whose security depends on a routing table.

## The decision cache, and the number hidden in it

```
cached decisions after two requests: 7
their TTL: 30s
```

Keyed by the **token**, not the user: two tokens for one person can carry
different scopes, and caching by user id lets a narrow token inherit a broad
one's verdict.

That TTL is the maximum time the gateway keeps letting a request through after
the answer changed — the same lesson as `redis_cache_lab`'s scenario 05, on the
authorization side. Allows and denies should not share it: a stale allow is a
security incident, a stale deny is a support ticket, so this service gives denies
a longer TTL.

## The anomaly: demoting someone does not demote their token

```sql
UPDATE users SET role = 'member' WHERE id = 1;   -- Alice is no longer an admin
```

```
POST /events/11  as Alice  →  200, event published
```

Nothing is broken. The gateway trusts the **token**, and the token says admin. It
never reads the users table — that was the entire point of putting the role in
the token.

> **The revocation latency for a role change is the token's remaining lifetime.**

Not your cache TTL, not your deploy cycle: however long is left on a credential
you already handed out and cannot take back. An hour is a common default. Say
that number out loud in a security review and watch what happens.

## Three fixes, and what each really costs

### 1. Denylist the token id

```
./lab.sh revoke-token <jti>     →  next request: 401 "token revoked"
```

Immediate — **and it works only because the denylist is checked before the
decision cache**. Put those the other way around and revocation takes up to the
cache TTL to bite, which is the single most common way a revocation feature turns
out not to be one.

The cost: a lookup per request against shared state, on the request path. If that
store is down you choose between failing closed (an outage) and failing open (no
revocation). **Decide which before the incident.**

### 2. Short-lived tokens

No denylist, no lookup, no dependency. The cost is refresh traffic and a refresh
flow to operate — and the revocation latency is still the TTL. You made it small,
not zero.

### 3. Keep the claim out of the token

Zero latency, and you have re-introduced a database read on every request — which
is what the token was avoiding.

There is no free choice here, only a stated one.

## The rule that decides it for you

> **Put a claim in a token when it changes more slowly than the token lives.**

A user id qualifies. A tenant id qualifies. A role sometimes does. A permission
grant an admin can revoke this afternoon does not — which is why the ABAC grants
in this lab are read from the database on every request and never travel in the
token.

## Run it

```bash
./lab.sh run 04
./lab.sh as dan /datasets       # guest: public only
./lab.sh as carol /datasets     # a different tenant entirely
./lab.sh status                 # the decision cache and its TTLs
```

## The interview answer

> *"Where do you enforce authorization — the gateway or the service?"*

"Both, deciding different things. The gateway does route-level RBAC from the
token: cheap, central, fails fast, never touches a database. It *cannot* do
row-level ABAC, because that needs the grants table, and giving the gateway a
connection to it creates one component that can read every tenant's data and sits
on every request. So the service does the row filtering in the `WHERE` clause,
and it repeats the coarse check too — the gateway's copy is an optimisation, the
service's copy is the guarantee.

The part people miss is that putting the role in the token makes the revocation
latency equal to the token's remaining lifetime: demoting someone in the database
changes nothing until it expires. I fix that with a `jti` denylist checked
*before* the decision cache, or by keeping fast-changing claims out of the token.
The rule: a claim belongs in a token only if it changes more slowly than the
token lives."
