# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "redis"]
# ///
"""Scenario 04 — key strategy: collisions, rolling deploys, and over-broad keys."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import Lab, redis_client, reset  # noqa: E402

USER = 1

lab = Lab(
    "04 — KEY STRATEGY",
    "A key name is an API between services that never agreed to have one.",
)
reset()

IDENTITY = lab.session("idsv")  # the identity service
BILLING = lab.session("bill")  # a different team, same Redis cluster
rdb = redis_client()


# ---------------------------------------------------------------------------
lab.section("The collision: two services, one cluster, one obvious key name")
lab.note(
    """
    Both teams picked the shortest name that made sense to them. Neither team
    did anything wrong in isolation, and nobody reviews the other team's MRs.
    """
)

IDENTITY.cache_set("user:1", "admin", note="identity service: the role, as a string")
BILLING.cache_set(
    "user:1",
    json.dumps({"plan": "pro", "seats": 12}),
    note="billing service: a JSON blob",
)

what_identity_reads = IDENTITY.cache_get("user:1")
lab.broke(
    what_identity_reads != "admin",
    f"the identity service asked for a role and got {what_identity_reads!r}",
    """
    Best case this raises and you get a 500 with a stack trace pointing at a
    line that is not the bug. Worst case both sides store strings, the parse
    succeeds, and one service silently authorises using another service's data.

    Note there is no error ANYWHERE in Redis. `SET` overwriting a key of a
    different shape is not an error condition; it is the entire point of SET.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The convention that prevents it")
lab.note(
    """
    identity:v1:user:42:profile
    └──┬───┘ └┬┘ └──┬───┘ └──┬──┘
       │      │     │        └── projection: WHICH view of the entity
       │      │     └─────────── entity:id, in that order
       │      └───────────────── schema version of the VALUE's shape
       └──────────────────────── owning service — one team owns this prefix

    Every segment earns its place:

    service      One cluster serves many services. Without a prefix, two teams
                 pick `user:42` and corrupt each other, exactly as above.
                 Owning a prefix is owning the data. It also makes
                 `--scan --pattern 'identity:*'` a meaningful question, and it
                 gives you a per-team memory number when the cluster fills up.

    v1           The shape of the VALUE, not the value's version. See below —
                 this one is invisible until your first rolling deploy.

    entity:id    In that order, so the tree in RedisInsight is browsable and
                 `SCAN MATCH identity:v1:user:*` means something.

    projection   `:profile` and `:permissions` have different TTLs and are
                 invalidated by different writes. One key holding both means
                 every write invalidates everything — proven below.

    And one rule with no segment of its own: NEVER put PII in a key. Keys show
    up in MONITOR output, SLOWLOG, `--scan` dumps, latency reports and error
    messages — all places with looser access control than the data itself.
    `identity:v1:user:42:profile`, never `identity:v1:user:alice@acme.test`.
    """
)


# ---------------------------------------------------------------------------
lab.section("Why the value's schema version is in the key")
lab.note(
    """
    You add `org_id` to the cached profile. For the ten minutes of a rolling
    deploy, old pods and new pods are both live and both writing.
    """
)

reset()
OLD = lab.session("old")
NEW = lab.session("new")

# Without the version segment, both generations share one key.
OLD.cache_set("identity:user:1:profile", json.dumps({"role": "guest"}), note="old pod writes")
raw = NEW.cache_get("identity:user:1:profile")
try:
    org = json.loads(raw)["org_id"]
    crashed = False
except KeyError:
    crashed = True
    NEW.say("KeyError: 'org_id' — new code, old value, 500 for the user")

lab.broke(
    crashed,
    "a new pod read a value an old pod wrote and could not parse it",
    """
    This is a bad one to debug because it is intermittent BY DESIGN: it happens
    only while both generations are live, and it stops the moment the deploy
    finishes — right around the time you finish reading the logs.
    """,
)

reset()
OLD.cache_set("identity:v1:user:1:profile", json.dumps({"role": "guest"}), note="old pod, v1")
NEW.cache_set(
    "identity:v2:user:1:profile",
    json.dumps({"role": "guest", "org_id": 100}),
    note="new pod, v2",
)
v2 = json.loads(NEW.cache_get("identity:v2:user:1:profile"))
lab.held(
    v2.get("org_id") == 100,
    "with the version in the prefix, the two generations never meet",
    """
    The old keys are not migrated, deleted or backfilled. They simply stop
    being read and expire on their own. That is the cheapest data migration in
    this repository, and it is available only because the version is in the KEY
    rather than inside the value.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Why the projection is its own key")
lab.note(
    """
    One key holding the whole user — role, display name, everything — looks
    tidy and couples every write to every read.

    A display-name change is a cosmetic write. Watch what it costs when the
    role shares its key. `db_reads` counts SELECTs that reached Postgres.
    """
)

reset()
APP = lab.session("app")

# --- combined key -----------------------------------------------------------
APP.cache_set(
    "identity:v1:user:1",
    json.dumps({"role": "guest", "display_name": "Alice"}),
    note="one key for the whole entity",
)
APP.sql("UPDATE users SET display_name = %s WHERE id = %s", ("Alicia", USER))
APP.cache_del("identity:v1:user:1", note="the display-name write invalidates EVERYTHING")

before = APP.db_reads
APP.cache_get("identity:v1:user:1")
APP.say("permission check misses, because a cosmetic write evicted the role")
APP.sql("SELECT role FROM users WHERE id = %s", (USER,))
combined_reads = APP.db_reads - before

# --- split keys -------------------------------------------------------------
reset()
APP.cache_set("identity:v1:user:1:profile", json.dumps({"display_name": "Alice"}))
APP.cache_set("identity:v1:user:1:role", "guest")
APP.sql("UPDATE users SET display_name = %s WHERE id = %s", ("Alicia", USER))
APP.cache_del("identity:v1:user:1:profile", note="only the projection that changed")

before = APP.db_reads
role = APP.cache_get("identity:v1:user:1:role", note="permission check: still a HIT")
split_reads = APP.db_reads - before

lab.held(
    combined_reads == 1 and split_reads == 0 and role == "guest",
    f"combined key: {combined_reads} database read; split keys: {split_reads}",
    """
    Multiply by your write rate. If display names change 100×/s and every one
    of them evicts the role that every request needs, you have built a cache
    that converts cosmetic writes into authorisation load.

    The rule: one key per (entity, projection) — where a projection is a set of
    fields that are read together AND invalidated together. If two fields are
    invalidated by different writes, they are two keys.
    """,
)


# ---------------------------------------------------------------------------
lab.section("What a key costs, measured")

reset()
short = "i:1:u:1:p"
verbose = "identity-service:cache:version-1:entities:user:1:projections:profile"
rdb.set(short, "admin")
rdb.set(verbose, "admin")
short_bytes = rdb.memory_usage(short)
verbose_bytes = rdb.memory_usage(verbose)

lab.measure(f"{short!r}", f"{short_bytes} bytes")
lab.measure(f"{verbose!r}", f"{verbose_bytes} bytes")
lab.measure(
    "10 million keys, difference:",
    f"{(verbose_bytes - short_bytes) * 10_000_000 / 1024 / 1024:.0f} MB of RAM, "
    "storing nothing but names",
)

lab.held(
    verbose_bytes > short_bytes,
    "key names are stored in RAM too, and you pay for every character",
    """
    Not an argument for cryptic keys — a debuggable key name is worth real
    money, and at 10k keys this is noise. It is an argument for knowing the
    number before you make the tradeoff, and for putting the verbosity in the
    segments that pay for themselves rather than in decoration.
    """,
)

lab.takeaway(
    """
    "I namespace keys as service:schema_version:entity:id:projection. The
     service prefix stops two teams sharing a cluster from silently
     overwriting each other, and makes per-team memory measurable. The schema
     version is the value's shape, so a rolling deploy can't have new code
     parsing old values — and bumping it is the cheapest cache migration there
     is, because old keys just expire. The projection is separate because
     fields invalidated by different writes belong in different keys;
     otherwise a display-name change evicts the role every request needs.
     And never PII in a key — key names leak into MONITOR, SLOWLOG and error
     messages, which are all less protected than the data."
    """
)

rdb.close()
lab.finish()
