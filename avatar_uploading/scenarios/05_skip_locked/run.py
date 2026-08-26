# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]"]
# ///
"""Scenario 05 — a job queue: no lock vs FOR UPDATE vs FOR UPDATE SKIP LOCKED."""

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import psycopg  # noqa: E402

from lab.harness import DSN, Lab, reset  # noqa: E402

CLAIM = "SELECT id FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1"

lab = Lab(
    "05 — FOR UPDATE SKIP LOCKED",
    "The difference between a queue that uses its workers and one that doesn't.",
)
reset()

A = lab.session("A")
B = lab.session("B")


def requeue():
    A.conn.execute("UPDATE jobs SET state = 'pending', locked_by = NULL")


# ---------------------------------------------------------------------------
lab.section("No locking: every worker claims the same job")

A.run("BEGIN")
a_job = A.scalar(CLAIM)
B.run("BEGIN")
b_job = B.scalar(CLAIM)

lab.broke(
    a_job == b_job,
    f"both workers claimed job {a_job} — the work will be done twice",
    "A plain SELECT takes no lock and promises nothing about who else is "
    "reading the same row. If the job charges a card, it gets charged twice.",
)

A.run("UPDATE jobs SET state = 'running', locked_by = 'A' WHERE id = %s", (a_job,))
A.run("COMMIT")
B.run("UPDATE jobs SET state = 'running', locked_by = 'B' WHERE id = %s", (b_job,))
B.run("COMMIT", note="silently overwrites A's claim")

owner = A.scalar("SELECT locked_by FROM jobs WHERE id = %s", (a_job,))
untouched = A.scalar("SELECT count(*) FROM jobs WHERE state = 'pending'")
lab.broke(
    owner == "B" and untouched == 49,
    f"job {a_job} says locked_by={owner!r}, and {untouched} jobs went unclaimed",
)


# ---------------------------------------------------------------------------
lab.section("FOR UPDATE: correct, and effectively single-threaded")
requeue()

A.run("BEGIN")
a_job = A.scalar(CLAIM + " FOR UPDATE")

B.run("BEGIN")
pending = B.spawn(CLAIM + " FOR UPDATE")
lab.broke(
    pending.still_blocked_after(1.0),
    f"B blocks on job {a_job} while 49 other jobs sit free",
    "B is not waiting for work. It is waiting for one specific row. Adding "
    "workers just makes the queue behind that row longer — a lock convoy.",
)

A.run("UPDATE jobs SET state = 'running', locked_by = 'A' WHERE id = %s", (a_job,))
A.run("COMMIT")
pending.wait()
b_job = pending.rows[0][0]
lab.held(
    b_job != a_job,
    f"once A commits, B re-runs its scan and correctly gets job {b_job}",
    "The result is right. The throughput is one worker's worth.",
)
B.run("ROLLBACK")


# ---------------------------------------------------------------------------
lab.section("FOR UPDATE SKIP LOCKED: correct and parallel")
requeue()

A.run("BEGIN")
a_job = A.scalar(CLAIM + " FOR UPDATE SKIP LOCKED")

B.run("BEGIN")
pending = B.spawn(CLAIM + " FOR UPDATE SKIP LOCKED")
lab.held(
    not pending.still_blocked_after(1.0),
    "B does not block at all",
)
pending.wait()
b_job = pending.rows[0][0]
lab.held(
    b_job != a_job,
    f"A holds job {a_job}, B took job {b_job} — two workers, two jobs, no waiting",
    "The scan stepped over the row A holds and returned the next free one.",
)
A.run("ROLLBACK")
B.run("ROLLBACK")


# ---------------------------------------------------------------------------
lab.section("The same thing, measured: 5 workers, 0.3s of work each")
lab.note(
    """
    Each worker claims a job, holds it for 300ms, and commits. With a queue
    that scales, five workers finish in about the time of one job.
    """
)
requeue()

HOLD = 0.3


def worker(clause: str, name: str, claimed: list, lock: threading.Lock):
    with psycopg.connect(DSN, autocommit=True, application_name=f"w_{name}") as c:
        c.execute("BEGIN")
        row = c.execute(CLAIM + clause).fetchone()
        if row:
            c.execute(
                "UPDATE jobs SET state='running', locked_by=%s WHERE id=%s", (name, row[0])
            )
            time.sleep(HOLD)  # stand-in for the actual work
            with lock:
                claimed.append(row[0])
        c.execute("COMMIT")


def race(clause: str) -> tuple[float, list]:
    requeue()
    claimed, lock = [], threading.Lock()
    threads = [
        threading.Thread(target=worker, args=(clause, f"w{i}", claimed, lock))
        for i in range(5)
    ]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.monotonic() - start, claimed


for_update_secs, fu_jobs = race(" FOR UPDATE")
print(f"  {'FOR UPDATE':<26} {for_update_secs:5.2f}s   jobs claimed: {sorted(fu_jobs)}")

skip_secs, sl_jobs = race(" FOR UPDATE SKIP LOCKED")
print(f"  {'FOR UPDATE SKIP LOCKED':<26} {skip_secs:5.2f}s   jobs claimed: {sorted(sl_jobs)}")

speedup = for_update_secs / skip_secs if skip_secs else 0
lab.broke(
    for_update_secs > HOLD * 4,
    f"FOR UPDATE took {for_update_secs:.2f}s — the workers ran one after another",
    f"5 workers x {HOLD}s of work, serialized.",
)
lab.held(
    skip_secs < HOLD * 2,
    f"SKIP LOCKED took {skip_secs:.2f}s — {speedup:.1f}x faster, workers ran in parallel",
)
lab.held(
    len(set(sl_jobs)) == 5,
    f"and all 5 workers got distinct jobs: {sorted(sl_jobs)}",
)

lab.note(
    """
    The cost to name out loud: SKIP LOCKED returns whatever is unlocked right
    now, so it gives up both snapshot consistency and strict FIFO ordering by
    design. Perfect for a queue, wrong for anything whose answer must be
    reproducible.
    """
)


lab.takeaway(
    """
    "Our queue was SELECT ... FOR UPDATE LIMIT 1, which was correct but
     effectively single-threaded — every worker queued on the same head row, so
     adding workers changed nothing. FOR UPDATE SKIP LOCKED lets each worker step
     over rows another worker already holds, so throughput scales with workers.
     The tradeoff is that it gives up snapshot consistency and strict FIFO by
     design — it belongs in a queue and nowhere near a report."
    """
)
lab.finish()
