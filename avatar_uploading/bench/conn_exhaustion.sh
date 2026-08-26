#!/usr/bin/env bash
# What "the database fell over at 10k CCU" usually actually means.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DSN="${LAB_DSN:-postgresql://lab:lab@localhost:5433/lab}"
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

LIMIT=$(psql "$DSN" -X -tAc 'SHOW max_connections' | tr -d ' ')
bold "Server limit: max_connections = $LIMIT"
psql "$DSN" -X -q <<'SQL'
SELECT count(*) AS in_use,
       count(*) FILTER (WHERE state = 'active')              AS active,
       count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
FROM pg_stat_activity WHERE datname = current_database();
SQL

echo
bold "1. Drive 100 pgbench clients at a $LIMIT-connection server"
echo "   Expect: pgbench cannot even start — the server refuses the connections."
echo
if pgbench -n -f bench/avatar_append.sql -c 100 -j 8 -T 5 "$DSN" 2>&1 | tail -6; then
    echo
    echo "   (It survived. Your server is more generous than expected —"
    echo "    lower max_connections in docker-compose.yml to force the failure.)"
fi

echo
bold "2. The same 100 requests through a client-side pool of 20"
echo "   Expect: nothing refused. The excess waits in the client instead."
echo
command -v uv >/dev/null || { echo "uv not found; skipping the pooled demo."; exit 1; }
uv run --quiet bench/pool_demo.py

echo
bold "Why this is the answer to \"where did it break first?\""
cat <<'EOF'
  Connections are the resource that runs out first, and usually not because of
  the endpoint that exhausts them. Every request BLOCKED on a row lock is still
  holding its connection (scenario 07). So a single contended row converts into
  pool exhaustion, and the symptom shows up on unrelated endpoints that simply
  could not get a connection.

  That is why "we scaled the API tier" often makes it worse: more app servers
  means more concurrent connections against the same fixed server limit.

  The three fixes, in the order they actually matter:
    1. Shorten transactions   — never hold one across a network call
    2. Pool connections       — bound the queue in the client (pgbouncer, or your driver)
    3. Remove the contention  — the schema change scenario 03 is about

  Raising max_connections is not on that list. Each backend costs memory and
  adds work to every snapshot; past a few hundred you lose throughput.
EOF
