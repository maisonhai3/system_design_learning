-- Who is blocked, and who is blocking them.
-- Used by `./lab.sh watch`. Run it in a third terminal while a scenario hangs.
\pset border 2
SELECT
    a.pid,
    a.application_name                          AS who,
    a.state,
    a.wait_event_type || ':' || a.wait_event    AS waiting_on,
    pg_blocking_pids(a.pid)                     AS blocked_by,
    now() - a.xact_start                        AS xact_age,
    left(regexp_replace(a.query, '\s+', ' ', 'g'), 60) AS query
FROM pg_stat_activity a
WHERE a.datname = current_database()
  AND a.pid <> pg_backend_pid()
  AND (a.state <> 'idle' OR a.xact_start IS NOT NULL)
ORDER BY cardinality(pg_blocking_pids(a.pid)) DESC, a.pid;
