# `FOR UPDATE SKIP LOCKED` — a job queue that actually uses its workers

**The claim you are practising:** *"A `SELECT … LIMIT 1` queue without locking
hands the same job to every worker. With `FOR UPDATE` it is correct but
single-threaded, because every worker queues on the same row. `SKIP LOCKED`
makes each worker step over the locked rows and take the next free one."*

## Three versions of the same queue

**No locking — duplicate work.** Every worker runs `SELECT id FROM jobs WHERE
state = 'pending' ORDER BY id LIMIT 1`, and every worker gets job 1. They all
process it. If the job charges a credit card, you have just charged it five
times. Nothing errors; the second `UPDATE` simply overwrites the first.

**`FOR UPDATE` — correct, but you are paying for workers you cannot use.**
Worker A locks job 1. Worker B runs the identical query and *blocks on job 1*,
even though jobs 2 through 50 are sitting there free. B waits out the entire
duration of A's transaction, then re-runs its scan and takes job 2. Correct
results, one job at a time, and adding workers does nothing but lengthen the
queue behind row 1. This is a **lock convoy**.

**`FOR UPDATE SKIP LOCKED` — correct and parallel.** B's scan skips any row
already locked and returns the next unlocked one. B gets job 2 immediately,
without waiting. Five workers claim five different jobs at once.

## The tradeoff you must be able to name

`SKIP LOCKED` deliberately breaks the usual guarantee that a `SELECT` sees a
consistent snapshot — it returns *whatever happens to be unlocked right now*,
which means two runs of the same query can return different rows for reasons
that have nothing to do with committed data. That is exactly what you want for
a work queue and exactly what you do not want for a report. Never put it in a
query whose answer is supposed to be reproducible.

Also: skipping is not ordering. With `SKIP LOCKED` you lose any promise of
strict FIFO processing, because a worker steps over rows another worker is
holding. If strict ordering matters, you need a different design (partition the
queue by key and let one worker own each partition).

## Run it

    ./lab.sh run 05
    ./lab.sh a 05      # terminal 1
    ./lab.sh b 05      # terminal 2

## The interview version

> "Our queue was `SELECT … FOR UPDATE LIMIT 1`, which was correct but
> effectively single-threaded — every worker queued on the same head row, so
> adding workers changed nothing. `FOR UPDATE SKIP LOCKED` lets each worker step
> over rows that are already claimed and take the next free one, so throughput
> scales with workers. The catch is that it gives up snapshot consistency and
> strict FIFO by design, so it belongs in a queue and nowhere near a report."
