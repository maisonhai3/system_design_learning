"""The cache-aside implementations that the scenarios put under test.

One file, several strategies, deliberately side by side — because the whole
lesson is that they look nearly identical and behave completely differently.
Every one of them is a real pattern somebody is running in production right
now, including the two that are wrong.

Read them in order:

    write_del_before_commit   the instinct. Permanently poisons the cache.
    write_del_after_commit    the standard answer. Still loses a rarer race.
    write_versioned           the version-keyed answer. Loses nothing, costs a
                              column and a slightly larger keyspace.
    read_single_flight        not about correctness at all — about the herd
                              that arrives one microsecond after an eviction.

The seam: every method takes an optional `latch`. That is how a scenario forces
an interleaving instead of sleeping and hoping. See Latch in lab/harness.py.
"""

from __future__ import annotations

import time
import uuid

from lab.harness import DIM, Latch, Session

# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

SERVICE = "identity"
KEY_SCHEMA = "v1"


def user_key(user_id: int) -> str:
    """`identity:v1:user:42:profile`

    Four segments, each earning its place:

      identity   the owning service. One Redis cluster serves many services;
                 without this, two teams pick `user:42` and silently corrupt
                 each other. Ownership of a key prefix is ownership of the data.
      v1         the *shape* of the value, not the value's version. Change the
                 serialized fields and you bump this. During a rolling deploy
                 old and new pods are both live; without the bump, new pods
                 write a shape old pods cannot parse. Bumping the prefix is the
                 cheapest cache migration there is — the old keys simply expire.
      user:42    entity and id, in that order, so `SCAN MATCH identity:v1:user:*`
                 is a meaningful query and RedisInsight renders a tree.
      profile    which projection. `:profile` and `:permissions` expire at
                 different rates and are invalidated by different writes; one
                 key holding both means every write invalidates everything.
    """
    return f"{SERVICE}:{KEY_SCHEMA}:user:{user_id}:profile"


def versioned_user_key(user_id: int, version: int) -> str:
    """`identity:v1:user:42:profile@7` — the row's write counter in the key.

    Now a stale writer physically cannot overwrite a fresh entry, because it is
    holding a different key. See scenario 03.
    """
    return f"{user_key(user_id)}@{version}"


def lock_key(user_id: int) -> str:
    return f"{SERVICE}:{KEY_SCHEMA}:lock:user:{user_id}:profile"


DEFAULT_TTL = 300  # 5 minutes. Scenario 02 is about why this is not optional.


# ---------------------------------------------------------------------------
# The source of truth
# ---------------------------------------------------------------------------


def load_role_from_db(s: Session, user_id: int, slow_ms: int = 0) -> str | None:
    """The read the cache exists to avoid.

    `slow_ms` makes it as slow as the real query it stands in for. A cache
    around a 0.2ms query is not a cache, it is a second source of truth you
    now have to keep consistent — and most cache incidents start there.
    """
    if slow_ms:
        s.sql("SELECT pg_sleep(%s)", (slow_ms / 1000.0,), note=f"stands in for a {slow_ms}ms query")
    return s.scalar("SELECT role FROM users WHERE id = %s", (user_id,))


def load_role_and_version(s: Session, user_id: int) -> tuple[str, int]:
    rows = s.sql("SELECT role, version FROM users WHERE id = %s", (user_id,))
    return rows[0][0], rows[0][1]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_cache_aside(
    s: Session,
    user_id: int,
    ttl: int | None = DEFAULT_TTL,
    slow_ms: int = 0,
    latch: Latch | None = None,
) -> str | None:
    """Cache aside, the textbook version.

    GET → hit? return. miss? read the database, SET, return.

    The window that ruins your week is between the database read and the SET.
    Nothing in this function holds a lock across it, so anything at all can
    happen in between — including the write that makes the value we are about
    to cache obsolete.
    """
    key = user_key(user_id)
    cached = s.cache_get(key)
    if cached is not None:
        s.say("cache HIT — the database is never touched")
        return cached

    s.say("cache MISS — falling through to Postgres")
    role = load_role_from_db(s, user_id, slow_ms=slow_ms)

    if latch:
        # ← the window. Everything that goes wrong, goes wrong here.
        s.say("… paused, holding a value that is already going stale")
        latch.arrive("loaded")

    if role is not None:
        s.cache_set(key, role, ttl=ttl)
    return role


def read_versioned(
    s: Session,
    user_id: int,
    ttl: int | None = DEFAULT_TTL,
    latch: Latch | None = None,
) -> str | None:
    """Cache aside, keyed by the row version the value was read at.

    The read costs one extra column, not one extra round trip: `version` comes
    back from the same SELECT. The pointer lookup does cost a second GET, which
    is the honest price — see scenario 03 for when that price is worth paying.
    """
    pointer = user_key(user_id)  # holds the *current* version number
    version = s.cache_get(pointer)
    if version is not None:
        cached = s.cache_get(versioned_user_key(user_id, int(version)))
        if cached is not None:
            s.say("cache HIT")
            return cached

    s.say("cache MISS — reading role AND version in one statement")
    role, version_now = load_role_and_version(s, user_id)

    if latch:
        s.say("… paused, holding a value that is already going stale")
        latch.arrive("loaded")

    # Write the value under the version it was actually read at. A writer that
    # committed while we were paused has already bumped the row's version, so
    # this SET lands on a key nobody will ever look up again. It is garbage,
    # not corruption — and its TTL collects it.
    s.cache_set(versioned_user_key(user_id, version_now), role, ttl=ttl)
    # Only advance the pointer if it still names the version we read, or an
    # older one. SET with no guard here would re-introduce the whole bug.
    _advance_pointer(s, pointer, version_now, ttl)
    return role


def _advance_pointer(s: Session, pointer: str, version: int, ttl: int | None) -> None:
    """Move the pointer forward, never backward.

    Lua because it must be atomic: GET-then-SET from the client is the same
    read-modify-write race we are trying to fix, just moved into Redis. Redis
    runs a script to completion with nothing interleaved, which is the whole
    reason EVAL exists.
    """
    script = """
    local current = redis.call('GET', KEYS[1])
    if current and tonumber(current) >= tonumber(ARGV[1]) then
      return 0
    end
    if ARGV[2] == '0' then
      redis.call('SET', KEYS[1], ARGV[1])
    else
      redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    end
    return 1
    """
    moved = s.redis.eval(script, 1, pointer, str(version), str(ttl or 0))
    s.log(
        DIM(f"EVAL advance_pointer({pointer}, {version})"),
        DIM("→ ") + ("moved" if moved else "refused: pointer is already newer"),
    )


def read_single_flight(
    s: Session,
    user_id: int,
    ttl: int | None = DEFAULT_TTL,
    slow_ms: int = 0,
    lock_ttl_ms: int = 2000,
) -> str | None:
    """Cache aside, but only one caller per key is allowed to miss at a time.

    `SET key value NX PX ms` is the whole mechanism: atomic test-and-set with a
    self-healing expiry, so a holder that crashes cannot wedge the key forever.

    Losers here wait and retry. That is the right call for a read a user is
    blocking on. The alternative — losers serve the stale value and one winner
    refreshes in the background — is better for a dashboard and worse for a
    permission check. Which one you pick is a product decision, not a Redis one.
    """
    key = user_key(user_id)
    cached = s.cache_get(key)
    if cached is not None:
        return cached

    token = uuid.uuid4().hex
    got = s.redis.set(lock_key(user_id), token, nx=True, px=lock_ttl_ms)
    if not got:
        # Someone else is already paying for this miss. Wait for their answer.
        deadline = time.monotonic() + (lock_ttl_ms / 1000.0) + 1.0
        while time.monotonic() < deadline:
            cached = s.redis.get(key)
            if cached is not None:
                return cached
            time.sleep(0.005)
        # Leader died or took too long: fall through rather than fail the
        # request. A cache must never be able to turn into an outage.
        return load_role_from_db(s, user_id, slow_ms=slow_ms)

    try:
        role = load_role_from_db(s, user_id, slow_ms=slow_ms)
        if role is not None:
            s.redis.set(key, role, ex=ttl)
        return role
    finally:
        _release_lock(s, lock_key(user_id), token)


def _release_lock(s: Session, key: str, token: str) -> None:
    """Delete the lock only if we still hold it.

    A plain DEL is a real bug: if our work overran the lock TTL, the lock has
    already been handed to someone else, and our DEL frees *their* lock. The
    compare-and-delete has to be atomic, so it is a script.
    """
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    s.redis.eval(script, 1, key, token)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def write_del_before_commit(
    s: Session, user_id: int, new_role: str, latch: Latch | None = None
) -> None:
    """WRONG. Invalidate first, commit second.

    The instinct is sound: "shrink the window where the cache disagrees with
    the database." The result is the opposite. Between the DEL and the COMMIT
    the database still holds the OLD value and the cache holds nothing, so any
    reader that misses in that window reads the old value and caches it — after
    the DEL that was supposed to remove it. The window is milliseconds; the
    damage lasts until the TTL, or forever without one.
    """
    s.sql("BEGIN")
    s.sql("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
    s.cache_del(user_key(user_id), note="← before COMMIT. This is the bug.")
    if latch:
        # The window is not "microseconds". It is however long the rest of this
        # transaction takes: more statements, a slow constraint check, a lock
        # wait. Anything that makes a transaction slow makes this bug likelier.
        s.say("… still inside the transaction; the row is still 'guest' to everyone else")
        latch.arrive("invalidated")
    s.sql("COMMIT")


def write_del_after_commit(
    s: Session, user_id: int, new_role: str, latch: Latch | None = None
) -> None:
    """The standard answer, and right for most services.

    Commit first, then invalidate. The cache is stale for the microseconds
    between the two, which is a bounded, self-healing inconsistency. Compare
    that with the unbounded one above: bounded staleness is a design decision,
    unbounded staleness is an incident.
    """
    s.sql("BEGIN")
    s.sql("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
    s.sql("COMMIT")
    if latch:
        s.say("… committed. The database is correct; the cache is not, yet.")
        latch.arrive("committed")
    s.cache_del(user_key(user_id), note="← after COMMIT")


def write_versioned(
    s: Session, user_id: int, new_role: str, latch: Latch | None = None
) -> None:
    """Commit, then publish the new version — no deletion involved.

    The trigger bumps `version` inside the same transaction as the role change,
    so the two can never disagree. Publishing the new pointer makes every older
    entry unreachable at once, which is what "invalidate" was always trying to
    approximate.
    """
    s.sql("BEGIN")
    rows = s.sql(
        "UPDATE users SET role = %s WHERE id = %s RETURNING version",
        (new_role, user_id),
    )
    new_version = rows[0][0]
    s.sql("COMMIT")
    if latch:
        s.say("… committed at version %d" % new_version)
        latch.arrive("committed")
    s.cache_set(versioned_user_key(user_id, new_version), new_role, ttl=DEFAULT_TTL)
    _advance_pointer(s, user_key(user_id), new_version, DEFAULT_TTL)
