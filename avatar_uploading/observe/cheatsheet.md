# Observing contention — the queries to know by heart

If you can only remember one thing from this file, remember that
**`wait_event_type` + `wait_event` in `pg_stat_activity` tells you what a
backend is actually stuck on**, and `pg_blocking_pids()` tells you who is
holding it up. Together they are the answer to *"how did you know it was the
database and not the app?"*

## Who is blocked right now

    ./lab.sh watch          -- runs observe/blocking.sql on a 2s refresh

```sql
SELECT pid, application_name, wait_event_type || ':' || wait_event AS waiting_on,
       pg_blocking_pids(pid) AS blocked_by,
       now() - xact_start    AS xact_age
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

## Wait events worth recognising

| `wait_event_type:wait_event` | What it means | Which scenario |
|---|---|---|
| `Lock:transactionid` | Waiting for another transaction's **row** lock | 01, 03, 05, 07 |
| `Lock:tuple` | Queued behind others already waiting for the same row | 07 under load |
| `Lock:relation` | Waiting for a **table** lock — usually a DDL/`VACUUM FULL` collision | — |
| `LWLock:BufferContent` | Contention on a single **page** in shared buffers | 07 at high `-c` |
| `Client:ClientRead` | Postgres is waiting for **your application** to send something | idle-in-transaction |
| `IO:DataFileRead` | Genuinely reading from disk | — |
| *(null)* | Running, not waiting | — |

`Lock:transactionid` is row contention. `Client:ClientRead` inside an open
transaction means your app opened a transaction and then went off to do
something else — an HTTP call, most likely — while holding its locks. Those two
diagnoses lead to completely different fixes, which is why the wait event
matters more than the wait time.

## Connection pressure

```sql
SELECT current_setting('max_connections')::int                         AS limit,
       count(*)                                                        AS total,
       count(*) FILTER (WHERE state = 'active')                        AS active,
       count(*) FILTER (WHERE state = 'idle in transaction')           AS idle_in_txn
FROM pg_stat_activity;
```

`idle in transaction` is the number to watch. Each one holds every lock it has
taken and pins the oldest snapshot vacuum can clean up to — while doing
absolutely nothing. A handful of these is how a pool of 100 connections behaves
like a pool of 3.

    ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';

is the blunt instrument for it. The real fix is not opening a transaction
before an external call.

## Are my transactions long, or are my queries slow?

```sql
SELECT pid, state,
       now() - xact_start  AS transaction_age,
       now() - query_start AS current_query_age
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
ORDER BY xact_start;
```

A big `transaction_age` with a small `current_query_age` means fast queries
inside a long-lived transaction — the shape that causes lock contention without
showing up in slow-query logs at all. This is the one that fools people.

## What is this table costing me?

```sql
SELECT relname, n_tup_upd, n_tup_hot_upd, n_dead_tup, n_live_tup,
       pg_size_pretty(pg_relation_size(relid)) AS size
FROM pg_stat_user_tables ORDER BY n_tup_upd DESC;
```

`n_tup_hot_upd` close to `n_tup_upd` is the healthy case: heap-only tuple
updates skip index maintenance. A ratio near zero means every update is
rewriting index entries — see scenario 07, where indexing the updated column
takes HOT updates from 1979/2000 to exactly 0.

Note these stats are flushed **asynchronously**, so they lag a second or so
behind reality. Do not read them in a tight loop and conclude nothing happened.

## Locks held, in detail

```sql
SELECT l.pid, l.locktype, l.mode, l.granted, c.relname
FROM pg_locks l
LEFT JOIN pg_class c ON c.oid = l.relation
WHERE NOT l.granted OR l.locktype = 'transactionid'
ORDER BY l.granted, l.pid;
```

## The server log

    ./lab.sh logs -f

This lab sets `log_lock_waits = on` and `deadlock_timeout = 500ms`, so any wait
longer than 500ms is logged with the blocking PID, and every deadlock is logged
with the full cycle and both queries. In production, `log_lock_waits` is one of
the highest-value settings you can turn on — it costs nothing until something
is already going wrong.
