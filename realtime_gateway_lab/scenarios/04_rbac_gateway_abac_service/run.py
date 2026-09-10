# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "psycopg[binary]", "redis"]
# ///
"""Scenario 04 — what the gateway can decide, and what it must not pretend to."""

import hashlib
import hmac
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import (  # noqa: E402
    SIGNED,
    Lab,
    db,
    jti_of,
    redis_client,
    reset,
    token_for,
)

PAYROLL = 11
GATEWAY_SECRET = "dev-gateway-secret"

lab = Lab(
    "04 — RBAC AT THE GATEWAY, ABAC AT THE SERVICE",
    "The gateway has never seen your data. That is not a limitation to work around.",
)
reset()

rdb = redis_client()


def signed(subject: int, role: str, org: int) -> dict:
    message = f"{subject}|{role}|{org}".encode()
    return {
        "X-Auth-Subject": str(subject),
        "X-Auth-Role": role,
        "X-Auth-Org": str(org),
        "X-Auth-Signature": hmac.new(GATEWAY_SECRET.encode(), message, hashlib.sha256).hexdigest(),
    }


alice = lab.actor("alice", "alice")   # admin, org 100
bob = lab.actor("bob", "bob")         # member, org 100
dan = lab.actor("dan", "dan")         # guest, org 100
carol = lab.actor("carol", "carol")   # admin, org 200


# ---------------------------------------------------------------------------
lab.section("What the gateway CAN decide: the route, from the token")

refused = bob.json("POST", f"/events/{PAYROLL}")
lab.held(
    refused.get("error") == "forbidden",
    f"Bob is stopped at the gateway: {refused.get('reason')}",
    """
    This request never reached the API service. That is the case for coarse
    RBAC at the edge, and it is a good one:

      it is cheap        one signature check and a table lookup, no database;
      it fails fast      a request that cannot possibly succeed is not carried
                         through your service mesh to find out;
      it is central      the token format, the clock-skew allowance and the
                         denylist exist once, not once per service.

    Note also what is being decided: "may a token with this ROLE reach this
    METHOD and PATH". Nothing about rows, nothing about ownership, nothing that
    requires knowing what exists.
    """,
)


# ---------------------------------------------------------------------------
lab.section("What the gateway CANNOT decide: which rows")
lab.note(
    """
    Every one of these four requests passes exactly the same gateway check:
    GET /datasets is allowed for every role. The gateway's job is finished and
    it has decided nothing about what comes back.
    """
)

views = {}
for actor in (alice, bob, dan, carol):
    payload = actor.json("GET", "/datasets")
    views[actor.name] = [d["id"] for d in payload["datasets"]]
    lab.measure(f"{actor.name} sees:", str(views[actor.name]))

lab.held(
    views["alice"] == [10, 11, 12]
    and views["bob"] == [10, 12]
    and views["dan"] == [12]
    and views["carol"] == [13, 14],
    "same route, same RBAC verdict, four different result sets",
    """
    To produce any one of those lists you must join grants against dataset
    classifications — you must know what rows exist and who was granted what.
    The gateway has never seen that table, and giving it a connection so it
    could would make it an upstream service with a gateway's blast radius: one
    component that can read every tenant's data and is on the path of every
    request.

    So the division is not a compromise, it is the only correct one:

      RBAC, coarse, at the edge     from the token. Route-level. Cheap.
      ABAC, row-level, in the service  from the data. In the WHERE clause.

    And "in the WHERE clause" is load-bearing. Filtering after fetching means
    the restricted rows were on the wire, in memory, and one logger.debug from
    your log aggregator.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The policy exists twice. A test is what keeps them the same policy.")
lab.note(
    """
    The row filter is written in SQL (a WHERE clause, so restricted rows are
    never fetched) and the same rule is written in Python (so the fan-out
    worker and the pointer dereference can evaluate it without a query).

    Two implementations of one policy is a drift waiting to happen — and it
    DID happen while this lab was being written: the Python version excluded
    guests from internal rows and the SQL version did not, so Dan could list a
    dataset the policy said he could not see.

    Nobody decides to have two policies. Somebody adds a clause to the one
    they have open. The defence is not discipline, it is this:
    """
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))
from app.domain.entities import Dataset, Subject  # noqa: E402
from app.usecases import authorize  # noqa: E402

disagreements = []
with db() as conn:
    users = conn.execute("SELECT id, role, org_id FROM users ORDER BY id").fetchall()
    datasets = conn.execute(
        "SELECT id, org_id, name, classification FROM datasets ORDER BY id"
    ).fetchall()
    for uid, role, org in users:
        sql_visible = {
            row[0]
            for row in conn.execute(
                "SELECT d.id FROM datasets d JOIN users u ON u.id = %s "
                "WHERE d.org_id = u.org_id AND u.org_id = %s AND ("
                "  d.classification = 'public'"
                "  OR (d.classification = 'internal' AND u.role <> 'guest')"
                "  OR EXISTS (SELECT 1 FROM dataset_grants g "
                "             WHERE g.dataset_id = d.id AND g.user_id = u.id))",
                (uid, org),
            ).fetchall()
        }
        granted = frozenset(
            row[0]
            for row in conn.execute(
                "SELECT dataset_id FROM dataset_grants WHERE user_id = %s", (uid,)
            ).fetchall()
        )
        subject = Subject(uid, role, org, provenance="test")
        for did, dorg, dname, dclass in datasets:
            python_says = authorize.may_see(
                subject, Dataset(did, dorg, dname, dclass), granted
            )
            sql_says = did in sql_visible
            if python_says != sql_says:
                disagreements.append((uid, did, python_says, sql_says))

lab.measure(
    "(subject, dataset) pairs checked:", str(len(users) * len(datasets))
)
lab.held(
    not disagreements,
    "the SQL policy and the Python policy agree on every pair",
    f"""
    Disagreements: {disagreements or 'none'}

    This test is worth more than the code it checks. It is cheap — a cross
    product of the seed data — it runs in CI, and it fails on the pull request
    that introduces the drift rather than in the incident three months later.

    If you can only have one authorization test, have this one.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Why the service repeats the check the gateway already made")
lab.note(
    """
    The publish endpoint re-checks the admin role even though the gateway
    already did. That looks like duplication until you go around the gateway
    with a perfectly valid, correctly signed MEMBER assertion.
    """
)

direct = lab.actor("bypass", None, base=SIGNED)
blocked = direct.json("POST", f"/events/{PAYROLL}", headers=signed(2, "member", 100))

lab.held(
    blocked.get("detail") == "publishing requires the admin role",
    "the service refused on its own authority, with the gateway not involved",
    """
    The two checks are not duplicates, they are different claims:

      the gateway's   is an OPTIMISATION — reject early, cheaply, centrally.
      the service's   is the GUARANTEE — it holds no matter how the request
                      arrived.

    Delete the gateway's and you get slower; delete the service's and you get
    a system whose security depends on a routing table.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The decision cache, and the number hidden inside it")

reset()
fresh = lab.actor("alice-2", "alice")
first = fresh.request("GET", "/datasets")
second = fresh.request("GET", "/datasets")

cache_keys = sorted(rdb.scan_iter("authz:v1:tok:*"))
ttl = rdb.ttl(cache_keys[0]) if cache_keys else -2

lab.measure("cached decisions after two requests:", str(len(cache_keys)))
lab.measure("their TTL:", f"{ttl}s")

lab.held(
    len(cache_keys) >= 1 and ttl > 0,
    f"the RBAC verdict is cached per (token, method, path) for {ttl}s",
    """
    Keyed by the TOKEN, not the user: two tokens for one person can carry
    different scopes, and caching by user id would let a narrow token inherit a
    broad token's verdict.

    Now the number. That TTL is the maximum time the gateway will keep letting
    a request through after the answer has changed — see redis_cache_lab's
    scenario 05 for the same lesson on the data side. Allows and denies should
    not share it: a stale allow is a security incident, a stale deny is a
    support ticket, so this service gives denies a longer TTL than allows.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The anomaly: demoting someone does not demote their token")
lab.note(
    """
    Security asks you to remove Alice's admin rights. You do the obvious thing
    and change the row in Postgres.
    """
)

with db() as conn:
    conn.execute("UPDATE users SET role = 'member' WHERE id = 1")

still_admin = fresh.json("POST", f"/events/{PAYROLL}?mode=fanout")
lab.broke(
    "event_id" in still_admin,
    "Alice is a member in the database and is still publishing as an admin",
    """
    Nothing is broken. The gateway is doing exactly what it was built to do:
    it trusts the TOKEN, and the token says admin. It never reads the users
    table — that was the entire point of putting the role in the token.

    So the revocation latency for a role change is the token's REMAINING
    LIFETIME. Not your cache TTL, not your deploy cycle: however long is left
    on a credential you already handed out and cannot take back. An hour is a
    common default. Say that number out loud in a security review and watch
    what happens.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Three fixes, and what each one really costs")

# (1) Denylist the token id. Immediate, and needs a jti.
jti = jti_of(fresh.token)
rdb.set(f"authz:v1:revoked:{jti}", "1")
after_revoke = fresh.json("POST", f"/events/{PAYROLL}?mode=fanout")

lab.held(
    after_revoke.get("error") == "token revoked",
    f"denylisting jti={jti} stops it on the very next request",
    """
    Immediate, and it works only because the denylist is checked BEFORE the
    decision cache. Put those two the other way around — cache first, denylist
    second — and revocation takes up to the cache TTL to bite, which is the
    single most common way a revocation feature turns out not to be one.

    The cost: the gateway now needs a lookup per request against shared state,
    which is a new hard dependency on the request path. If that store is down,
    you choose between failing closed (an outage) and failing open (no
    revocation). Decide which BEFORE the incident.
    """,
)

# (2) Short-lived tokens. The cost is refresh traffic.
short = lab.actor("alice-3", "alice", ttl=1)
time.sleep(1.5)
expired = short.json("GET", "/datasets")
lab.held(
    expired.get("error") == "invalid or expired token",
    "a short-lived token expires on its own, with no shared state at all",
    """
    No denylist, no lookup, no dependency. The cost is refresh traffic and a
    refresh-token flow to run, and the revocation latency is still the TTL —
    you have made it small, not zero.

    (3) is the third option: keep the role OUT of the token and look it up per
    request. Zero latency, and you have re-introduced a database read on the
    path of every request — which is what the token was avoiding. There is no
    free choice here, only a stated one.
    """,
)

lab.note(
    """
    The rule that makes the choice for you:

      put a claim in a token when it changes more slowly than the token lives.

    A user id qualifies. A tenant id qualifies. A role sometimes qualifies. A
    permission grant that an admin can revoke this afternoon does not — which
    is why the ABAC grants in this lab are read from the database on every
    request and never travel in the token.
    """
)

lab.takeaway(
    """
    "The gateway decides route-level RBAC from the token — cheap, central,
     fails fast, and it never touches a database. It cannot decide row-level
     ABAC, because that needs the grants table, and giving the gateway a
     connection to it would create one component that can read every tenant's
     data and sits on every request. So the service does row filtering in the
     WHERE clause, and it repeats the coarse check too: the gateway's copy is
     an optimisation, the service's copy is the guarantee.

     The part people miss is that putting the role in the token makes the
     revocation latency equal to the token's remaining lifetime — demoting
     someone in the database changes nothing until their token expires. I fix
     that with a jti denylist checked BEFORE the decision cache, or by keeping
     fast-changing claims out of the token entirely. The rule I use: a claim
     belongs in a token only if it changes more slowly than the token lives."
    """
)

rdb.close()
lab.finish()
