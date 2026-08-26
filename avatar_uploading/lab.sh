#!/usr/bin/env bash
# Avatar Uploading Lab — one entry point for everything.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

DSN="${LAB_DSN:-postgresql://lab:lab@localhost:5433/lab}"
POOLED_DSN="${LAB_POOLED_DSN:-postgresql://lab:lab@localhost:6433/lab}"
export LAB_DSN="$DSN"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

need_uv()  { command -v uv  >/dev/null || die "uv not found. Install: https://docs.astral.sh/uv/"; }
need_psql(){ command -v psql >/dev/null || die "psql not found. Install postgresql-client."; }

scenario_dir() {
    local key="$1" match
    match=$(find scenarios -maxdepth 1 -type d -name "${key}*" | sort | head -1)
    [ -n "$match" ] || die "No scenario matching '$key'. Try: ./lab.sh list"
    echo "$match"
}

wait_ready() {
    # Deliberately probes over TCP from the HOST, not with `pg_isready` inside
    # the container. During first boot the entrypoint runs a temporary server
    # with listen_addresses='' to apply schema/, and pg_isready reports that
    # bootstrap server as ready — so an in-container check returns success
    # while the real server has not started listening on 5433 yet. Connecting
    # from outside is the only check that cannot see the bootstrap server.
    #
    # Two consecutive successes, because the very first TCP accept can still
    # land during the restart and get closed underneath us.
    local i hits=0
    for i in $(seq 1 90); do
        if psql "$DSN" -X -q -tAc 'SELECT 1' >/dev/null 2>&1; then
            hits=$(( hits + 1 ))
            [ "$hits" -ge 2 ] && return 0
        else
            hits=0
        fi
        sleep 1
    done
    die "postgres did not become ready in 90s. Check: ./lab.sh logs"
}

usage() {
cat <<'EOF'
Avatar Uploading Lab

  ./lab.sh up                 start postgres on localhost:5433 (schema + seed applied)
  ./lab.sh down               stop and delete the database
  ./lab.sh reset              re-apply schema + seed (fast; keeps the container)
  ./lab.sh status             show container state and connection counts
  ./lab.sh logs [-f]          postgres logs (this is where lock waits show up)

  ./lab.sh list               list scenarios
  ./lab.sh read <n>           print a scenario's README
  ./lab.sh a <n>              open session A: prints a.sql, then drops you into psql
  ./lab.sh b <n>              open session B: prints b.sql, then drops you into psql
  ./lab.sh run <n>            run the scenario's automated proof
  ./lab.sh run-all            run every scenario's proof; non-zero if any fails

  ./lab.sh psql [args...]     plain psql against the lab
  ./lab.sh watch              live view of blocked/blocking sessions (2s refresh)

  ./lab.sh bench              hot row vs spread row vs append-only, at -c 1/10/50/100
  ./lab.sh exhaust            drive 100 clients at a 50-connection server
  ./lab.sh pool up|down       start/stop pgbouncer on localhost:6433

Connection strings:
  direct  postgresql://lab:lab@localhost:5433/lab
  pooled  postgresql://lab:lab@localhost:6433/lab   (only after `./lab.sh pool up`)
EOF
}

cmd="${1:-help}"; shift || true

case "$cmd" in

up)
    docker compose up -d postgres
    wait_ready
    bold "Ready. Schema and seed applied."
    echo "  psql '$DSN'"
    echo "  ./lab.sh list"
    ;;

down)
    docker compose --profile pool down
    ;;

reset)
    need_psql
    psql "$DSN" -q -v ON_ERROR_STOP=1 -f schema/01_schema.sql -f schema/02_seed.sql
    bold "Reset: schema + seed re-applied."
    ;;

status)
    docker compose ps
    echo
    need_psql
    psql "$DSN" -X -q <<'SQL'
SELECT current_setting('max_connections') AS max_connections,
       (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS in_use;
SELECT relname AS table, n_live_tup AS approx_rows
FROM pg_stat_user_tables ORDER BY relname;
SQL
    ;;

logs)
    docker compose logs "$@" postgres
    ;;

list)
    bold "Scenarios"
    for d in scenarios/*/; do
        n=$(basename "$d")
        # First markdown heading of the README is the scenario's title.
        t=$(grep -m1 '^# ' "$d/README.md" 2>/dev/null | sed 's/^# //')
        printf '  %-20s %s\n' "$n" "$t"
    done
    echo
    echo "Read one with: ./lab.sh read 03"
    ;;

read)
    [ $# -ge 1 ] || die "usage: ./lab.sh read <n>"
    d=$(scenario_dir "$1")
    if command -v glow >/dev/null; then glow "$d/README.md"; else cat "$d/README.md"; fi
    ;;

a|b)
    need_psql
    [ $# -ge 1 ] || die "usage: ./lab.sh $cmd <n>"
    d=$(scenario_dir "$1")
    f="$d/$cmd.sql"
    [ -f "$f" ] || die "$f does not exist (this scenario has no manual $cmd session)."
    bold "=== $(basename "$d") — session ${cmd^^} ==="
    echo "Run these one block at a time. Watch the OTHER terminal after each."
    echo
    cat "$f"
    echo
    bold "--- psql starts now; paste the blocks above in order ---"
    # application_name shows up in pg_stat_activity and in the server log,
    # which is how `./lab.sh watch` tells A and B apart.
    PGAPPNAME="session_${cmd}" exec psql "$DSN" -v "PROMPT1=${cmd^^}> " -v "PROMPT2=${cmd^^}| "
    ;;

run)
    need_uv
    [ $# -ge 1 ] || die "usage: ./lab.sh run <n>"
    d=$(scenario_dir "$1"); shift
    exec uv run --quiet "$d/run.py" "$@"
    ;;

run-all)
    need_uv
    failed=()
    for d in scenarios/*/; do
        n=$(basename "$d")
        [ -f "$d/run.py" ] || continue
        echo
        bold "════════ $n ════════"
        if ! uv run --quiet "$d/run.py"; then failed+=("$n"); fi
    done
    echo
    if [ ${#failed[@]} -gt 0 ]; then
        die "FAILED: ${failed[*]}"
    fi
    bold "All scenarios reproduced and all fixes held."
    ;;

psql)
    need_psql
    exec psql "$DSN" "$@"
    ;;

watch)
    need_psql
    exec watch -n 2 psql "$DSN" -X -q -f observe/blocking.sql
    ;;

bench)
    exec bench/bench.sh "$@"
    ;;

exhaust)
    exec bench/conn_exhaustion.sh "$@"
    ;;

pool)
    sub="${1:-up}"
    case "$sub" in
        up)
            echo "Note: this pulls edoburu/pgbouncer and has not been verified here."
            echo "'./lab.sh exhaust' demonstrates the same lesson without it."
            docker compose --profile pool up -d pgbouncer
            bold "pgbouncer listening on localhost:6433 (transaction pooling, pool size 20)"
            echo "  psql '$POOLED_DSN'"
            ;;
        down) docker compose --profile pool stop pgbouncer ;;
        *)    die "usage: ./lab.sh pool up|down" ;;
    esac
    ;;

help|--help|-h) usage ;;
*) usage; die "unknown command: $cmd" ;;
esac
