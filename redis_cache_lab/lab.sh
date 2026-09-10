#!/usr/bin/env bash
# Redis Cache Lab — one entry point for everything.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

REDIS_PORT="${REDIS_PORT:-6380}"
POSTGRES_PORT="${POSTGRES_PORT:-5436}"
APP_PORT="${APP_PORT:-8001}"

DSN="${LAB_DSN:-postgresql://lab:lab@localhost:${POSTGRES_PORT}/lab}"
RURL="${LAB_REDIS_URL:-redis://localhost:${REDIS_PORT}/0}"
export LAB_DSN="$DSN" LAB_REDIS_URL="$RURL" APP_PORT

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

need_uv()    { command -v uv    >/dev/null || die "uv not found. Install: https://docs.astral.sh/uv/"; }
need_psql()  { command -v psql  >/dev/null || die "psql not found. Install postgresql-client."; }
need_rcli()  { command -v redis-cli >/dev/null || die "redis-cli not found. Install redis-tools."; }

scenario_dir() {
    local key="$1" match
    match=$(find scenarios -maxdepth 1 -type d -name "${key}*" | sort | head -1)
    [ -n "$match" ] || die "No scenario matching '$key'. Try: ./lab.sh list"
    echo "$match"
}

wait_ready() {
    # Probed over TCP from the HOST, not with an in-container check. During
    # first boot the Postgres entrypoint runs a temporary server with
    # listen_addresses='' to apply schema/, and pg_isready reports THAT server
    # as ready — so an in-container check succeeds while nothing is listening
    # on the published port yet. Connecting from outside is the only check that
    # cannot see the bootstrap server.
    local i pg=0 rd=0
    for i in $(seq 1 90); do
        psql "$DSN" -X -q -tAc 'SELECT 1' >/dev/null 2>&1 && pg=$(( pg + 1 )) || pg=0
        redis-cli -u "$RURL" ping >/dev/null 2>&1 && rd=$(( rd + 1 )) || rd=0
        # Two consecutive successes each: the first TCP accept can still land
        # during the entrypoint's restart and get closed underneath us.
        [ "$pg" -ge 2 ] && [ "$rd" -ge 2 ] && return 0
        sleep 1
    done
    die "stack did not become ready in 90s. Check: ./lab.sh logs"
}

usage() {
cat <<EOF
Redis Cache Lab

  ./lab.sh up                 start redis on :${REDIS_PORT} and postgres on :${POSTGRES_PORT}
  ./lab.sh down               stop and delete both
  ./lab.sh reset              re-apply schema + seed, FLUSHDB redis (fast)
  ./lab.sh status             container state, key count, memory, hit rate
  ./lab.sh logs [-f]          container logs
  ./lab.sh dblog              every query postgres ran (this is your cache miss log)

  ./lab.sh list               list scenarios
  ./lab.sh read <n>           print a scenario's README
  ./lab.sh run <n>            run the scenario's automated proof
  ./lab.sh run-all            run every proof; non-zero if any claim stops holding

  ./lab.sh cli [args...]      redis-cli against the lab
  ./lab.sh psql [args...]     psql against the lab
  ./lab.sh keys               dump the keyspace with TTLs
  ./lab.sh watch              live keyspace + hit-rate view (2s refresh)
  ./lab.sh spy                stream every command redis receives (MONITOR)
  ./lab.sh events             stream keyspace notifications (SET/DEL/EXPIRE/evict)

  ./lab.sh app                run the FastAPI playground on :${APP_PORT}
  ./lab.sh demo               scripted two-request race against the running app

Connection strings:
  redis     ${RURL}
  postgres  ${DSN}
EOF
}

cmd="${1:-help}"; shift || true

case "$cmd" in

up)
    docker compose up -d
    wait_ready
    bold "Ready. Schema and seed applied, Redis empty."
    echo "  redis-cli -u '$RURL'"
    echo "  ./lab.sh list"
    ;;

down)
    docker compose down -v
    ;;

reset)
    need_psql; need_rcli
    psql "$DSN" -q -v ON_ERROR_STOP=1 -f schema/01_schema.sql -f schema/02_seed.sql
    redis-cli -u "$RURL" flushdb >/dev/null
    bold "Reset: schema + seed re-applied, Redis flushed."
    ;;

status)
    docker compose ps
    echo
    need_rcli
    bold "Redis"
    redis-cli -u "$RURL" info stats  | grep -E 'keyspace_hits|keyspace_misses|expired_keys|evicted_keys' || true
    redis-cli -u "$RURL" info memory | grep -E 'used_memory_human|maxmemory_human|maxmemory_policy' || true
    echo "  dbsize: $(redis-cli -u "$RURL" dbsize)"
    echo
    bold "Postgres"
    psql "$DSN" -X -q -c "SELECT relname AS table, n_live_tup AS approx_rows FROM pg_stat_user_tables ORDER BY relname;"
    ;;

logs)
    docker compose logs "$@"
    ;;

dblog)
    # Every statement, with durations. The number of SELECTs here after a cache
    # invalidation is the number scenario 06 is about.
    docker compose logs postgres 2>&1 | grep -E 'LOG:  (statement|duration)' | tail -n "${1:-40}"
    ;;

list)
    bold "Scenarios"
    for d in scenarios/*/; do
        n=$(basename "$d")
        t=$(grep -m1 '^# ' "$d/README.md" 2>/dev/null | sed 's/^# //')
        printf '  %-24s %s\n' "$n" "$t"
    done
    echo
    echo "Read one with: ./lab.sh read 01"
    ;;

read)
    [ $# -ge 1 ] || die "usage: ./lab.sh read <n>"
    d=$(scenario_dir "$1")
    if command -v glow >/dev/null; then glow "$d/README.md"; else cat "$d/README.md"; fi
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

cli)
    need_rcli
    exec redis-cli -u "$RURL" "$@"
    ;;

psql)
    need_psql
    exec psql "$DSN" "$@"
    ;;

keys)
    need_rcli
    # SCAN, never KEYS — see scenario 07. Even here, where the keyspace is tiny
    # and it would not matter, because the habit is the point.
    redis-cli -u "$RURL" --scan --pattern '*' | sort | while read -r k; do
        printf '  %-52s ttl=%-6s %s\n' "$k" \
            "$(redis-cli -u "$RURL" ttl "$k")" \
            "$(redis-cli -u "$RURL" get "$k" 2>/dev/null || echo '<non-string>')"
    done
    ;;

watch)
    need_rcli
    exec watch -n 2 "redis-cli -u '$RURL' info stats | grep -E 'keyspace_hits|keyspace_misses|evicted'; echo; redis-cli -u '$RURL' --scan --pattern '*' | sort | head -40"
    ;;

spy)
    need_rcli
    bold "Every command Redis receives. Ctrl-C to stop."
    echo "(MONITOR costs real throughput — never leave it running against production.)"
    exec redis-cli -u "$RURL" monitor
    ;;

events)
    need_rcli
    bold "Keyspace notifications: SET, DEL, EXPIRE, and evictions. Ctrl-C to stop."
    exec redis-cli -u "$RURL" psubscribe '__keyevent@0__:*'
    ;;

app)
    need_uv
    bold "FastAPI playground on http://localhost:${APP_PORT}  (docs at /docs)"
    exec uv run --quiet app/serve.py
    ;;

demo)
    exec ./app/demo.sh "$@"
    ;;

help|--help|-h) usage ;;
*) usage; die "unknown command: $cmd" ;;
esac
