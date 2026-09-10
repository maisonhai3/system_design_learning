# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "redis"]
# ///
"""Scenario 03 — the race that delete-after-commit does not fix."""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.cache import (  # noqa: E402
    read_cache_aside,
    read_versioned,
    user_key,
    versioned_user_key,
    write_del_after_commit,
    write_versioned,
)
from lab.harness import Lab, Latch, redis_client, reset  # noqa: E402

USER = 1

lab = Lab(
    "03 — THE READ-REPOPULATE RACE",
    "The ordering is correct. The cache is still poisoned. Now what?",
)
reset()

W = lab.session("W")
R = lab.session("R")
rdb = redis_client()


# ---------------------------------------------------------------------------
lab.section("The anomaly: the stale reader gets the last word")
lab.note(
    """
    Scenario 01's fix is in place — COMMIT then DEL — and it will not help,
    because the problem is not the order of the writer's two statements. It is
    that the READER's SET happens last, and it is carrying a value it read
    before the commit.

    Interleaving:
      R  GET → miss
      R  SELECT → 'guest'            ← the value is correct at this instant
      W  BEGIN / UPDATE / COMMIT     ← and obsolete at this one
      W  DEL                         ← deletes nothing; the key is not there yet
      R  SET 'guest'                 ← lands after the DEL. Poisoned.
    """
)

latch = Latch()
reader = R.spawn(read_cache_aside, R, USER, latch=latch)
latch.wait_for("loaded")  # R holds 'guest' and has not written it yet

write_del_after_commit(W, USER, "admin")  # correct ordering, no help

latch.let_through("loaded")
reader.wait()

db_role = W.scalar("SELECT role FROM users WHERE id = %s", (USER,))
cached = rdb.get(user_key(USER))

lab.broke(
    db_role == "admin" and cached == "guest",
    f"correct ordering, still wrong: Postgres {db_role!r}, Redis {cached!r}",
    """
    Read the trace again and notice the DEL returned 0 — it deleted nothing,
    because at that instant there was nothing to delete. The writer did
    everything right and had no way to know it had achieved nothing.
    """,
)

lab.note(
    """
    The general shape, and the sentence worth memorising:

        THE LAST WRITER TO REDIS WINS, AND CACHE-ASIDE HANDS THE STALE READER
        A CHANCE TO BE LAST.

    Every real fix does one of exactly two things:
      (a) stop the stale reader from being last  — locks, double delete
      (b) make being last not matter             — version the key
    (a) is probabilistic or expensive. (b) is deterministic and nearly free.
    """
)


# ---------------------------------------------------------------------------
lab.section("Mitigation: the delayed double delete")
lab.note(
    """
    Delete once after the commit, then delete again a moment later, hoping the
    second one lands after any reader that was mid-flight. Very widely used.
    Read the assertion below carefully — it is a claim about THIS run, not
    about your production traffic.
    """
)

reset()
latch = Latch()
reader = R.spawn(read_cache_aside, R, USER, latch=latch)
latch.wait_for("loaded")

write_del_after_commit(W, USER, "admin")
latch.let_through("loaded")
reader.wait()
poisoned = rdb.get(user_key(USER))

W.say("… timer fires; second DEL")
time.sleep(0.2)
W.cache_del(user_key(USER), note="the 'double' in delayed double delete")

repaired = read_cache_aside(R, USER)

lab.held(
    poisoned == "guest" and repaired == "admin",
    "the second delete removed the poisoned entry",
    """
    And here is the part to say out loud before an interviewer says it for you:
    this works because the second DEL happened to land after the reader. The
    delay has to exceed the longest possible gap between a reader's SELECT and
    its SET — a number that includes GC pauses, a slow query, and a network
    hiccup, so it is not a number anyone can know.

    Delayed double delete lowers the probability. It does not close the race.
    It is a mitigation, and calling it a fix is how you fail the follow-up.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The fix: put the row's version in the key")
lab.note(
    """
    Stop trying to win the race. Make the race not matter.

    The value is stored under the version it was read at — `...:profile@7` —
    and a separate pointer key names the current version. A reader that was
    holding a pre-commit value writes it under the OLD version, where nothing
    will ever look it up again. The pointer only ever moves forward.

    Same interleaving as the anomaly above. Watch the last two lines.
    """
)

reset()
latch = Latch()
reader = R.spawn(read_versioned, R, USER, latch=latch)
latch.wait_for("loaded")  # R holds ('guest', version 1)

write_versioned(W, USER, "admin")  # commits at version 2, publishes @2

latch.let_through("loaded")
reader.wait()

pointer = rdb.get(user_key(USER))
after = read_versioned(R, USER)
db_role = W.scalar("SELECT role FROM users WHERE id = %s", (USER,))

lab.held(
    after == "admin" == db_role and pointer == "2",
    f"the pointer names version {pointer}, and reads return {after!r}",
    """
    The stale reader still wrote. It wrote to `...:profile@1`, which the
    pointer no longer names, so nobody will ever read it. Its attempt to move
    the pointer backwards was refused by the Lua script — atomically, because a
    GET-then-SET from the client would be the same read-modify-write race one
    layer down.
    """,
)

stale_key = versioned_user_key(USER, 1)
garbage = rdb.get(stale_key)
garbage_ttl = rdb.ttl(stale_key)
lab.held(
    garbage == "guest" and garbage_ttl > 0,
    f"the orphaned entry {stale_key} still exists, with ttl={garbage_ttl}s",
    """
    This is the cost, stated honestly: version-keying trades correctness for
    keyspace. Every superseded version lingers until its TTL. That is fine —
    they are unreachable, bounded by write rate × TTL, and reclaimed without
    anyone doing anything — but "it costs nothing" would be a lie, and the TTL
    is now load-bearing for MEMORY as well as for correctness.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The options, and when each is right")
lab.note(
    """
    delete-after-commit + TTL   The default. Wrong for a bounded window, on a
                                rare interleaving, and self-healing. Ship this
                                for profile data and move on.

    delayed double delete       Lowers the probability, cannot close the race,
                                needs a timer you cannot size correctly. Know
                                it, name it as a mitigation, don't defend it as
                                a fix.

    version-keyed entries       Closes the race. Costs a column, a second GET
                                on the read path, and superseded keys until TTL.
                                Right when a stale value is a SECURITY problem
                                rather than a cosmetic one.

    write-through, never on read  Readers never write to the cache, so the race
                                cannot exist. Costs a Redis write on every
                                database write, and a cold cache after every
                                invalidation. Good for small, hot, rarely-
                                written data — a permission matrix, a feature
                                flag set.

    don't cache it              Always on the list. A 0.3ms indexed lookup on a
                                warm Postgres does not need a cache, and a cache
                                you added for tidiness is a consistency bug you
                                volunteered for.
    """
)

lab.takeaway(
    """
    "Delete-after-commit fixes the common case, not the race. A reader that
     SELECTed before my commit and SETs after my delete re-poisons the cache,
     and the delete returns 0 so the writer never learns it achieved nothing.
     The reason is that the last writer to Redis wins and cache-aside lets the
     stale reader be last. Delayed double delete lowers the odds with a timer
     nobody can size. What actually closes it is putting the row's version in
     the key: the stale reader writes to a key no one will look up, and a Lua
     compare-and-set stops the pointer moving backwards. I pay for that in
     keyspace, and the TTL cleans it up."
    """
)

rdb.close()
lab.finish()
