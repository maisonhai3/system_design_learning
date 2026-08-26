#!/usr/bin/env bash
# Hot row vs spread row vs append-only, measured with pgbench.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DSN="${LAB_DSN:-postgresql://lab:lab@localhost:5433/lab}"
SECONDS_PER_RUN="${BENCH_SECONDS:-8}"
# Deliberately kept under max_connections=50. Going past the wall is a separate
# demo: ./lab.sh exhaust
CLIENTS="${BENCH_CLIENTS:-1 10 25 40}"

command -v pgbench >/dev/null || { echo "pgbench not found (apt install postgresql-client)"; exit 1; }

bold() { printf '\033[1m%s\033[0m\n' "$*"; }

run_one() {  # script, clients -> tps
    local script="$1" clients="$2" jobs out
    jobs=$(( clients < 4 ? clients : 4 ))
    # -n: our tables are not pgbench's, so there is nothing to vacuum.
    out=$(pgbench -n -f "$script" -c "$clients" -j "$jobs" -T "$SECONDS_PER_RUN" "$DSN" 2>&1) || {
        echo "ERR"; return
    }
    awk '/^tps/ { printf "%.0f", $3; exit }' <<<"$out"
}

bold "pgbench: ${SECONDS_PER_RUN}s per cell, max_connections=$(psql "$DSN" -X -tAc 'SHOW max_connections')"
echo "Each transaction: BEGIN; one write; COMMIT."
echo
printf '  %-9s %14s %14s %14s %12s\n' clients "hot row" "spread rows" "append-only" "hot vs spread"
printf '  %s\n' "$(printf '%.0s-' {1..68})"

for c in $CLIENTS; do
    hot=$(run_one bench/avatar_hot_row.sql "$c")
    spread=$(run_one bench/avatar_spread_row.sql "$c")
    append=$(run_one bench/avatar_append.sql "$c")
    if [[ "$hot" =~ ^[0-9]+$ && "$spread" =~ ^[0-9]+$ && "$spread" -gt 0 ]]; then
        ratio=$(awk -v a="$spread" -v b="$hot" 'BEGIN{printf "%.1fx", a/b}')
    else
        ratio="-"
    fi
    printf '  %-9s %14s %14s %14s %12s\n' "$c" "$hot" "$spread" "$append" "$ratio"
done

echo
bold "How to read this"
cat <<'EOF'
  The hot-row column should flatten out as clients rise — past a point, adding
  clients adds queueing, not throughput, because every one of them wants the
  same row lock. The spread and append columns should keep climbing, because
  the statement itself was never the problem.

  The last column is the number to quote: it is what the schema decision costs
  you, isolated from hardware, network and query cost, because all three
  columns run the same server with the same work per transaction.

  What to say about it in an interview: "per-row write throughput is
  1 / transaction_duration, so the ceiling on a hot row is set by how long you
  hold the transaction open, not by how many app servers you run."

  Then: ./lab.sh exhaust   -- what happens when the waiters eat the pool.
EOF
