#!/usr/bin/env bash
# Realtime Gateway Lab — one entry point for everything.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

REDIS_PORT="${REDIS_PORT:-6381}"
POSTGRES_PORT="${POSTGRES_PORT:-5437}"
GATEWAY_PORT="${GATEWAY_PORT:-8090}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8091}"
NGINX_PORT="${NGINX_PORT:-8092}"
API_DIRECT_PORT="${API_DIRECT_PORT:-8093}"
API_SIGNED_PORT="${API_SIGNED_PORT:-8096}"
AUTHZ_PORT="${AUTHZ_PORT:-8094}"
INSIGHT_PORT="${INSIGHT_PORT:-8097}"

DSN="${LAB_DSN:-postgresql://lab:lab@localhost:${POSTGRES_PORT}/lab}"
RURL="${LAB_REDIS_URL:-redis://localhost:${REDIS_PORT}/0}"
GW="http://localhost:${GATEWAY_PORT}"
DIRECT="http://localhost:${API_DIRECT_PORT}"
export LAB_DSN="$DSN" LAB_REDIS_URL="$RURL" GATEWAY_PORT NGINX_PORT API_DIRECT_PORT API_SIGNED_PORT AUTHZ_PORT INSIGHT_PORT

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

need_uv()   { command -v uv   >/dev/null || die "uv not found. Install: https://docs.astral.sh/uv/"; }
need_psql() { command -v psql >/dev/null || die "psql not found. Install postgresql-client."; }
need_rcli() { command -v redis-cli >/dev/null || die "redis-cli not found. Install redis-tools."; }

scenario_dir() {
    local key="$1" match
    match=$(find scenarios -maxdepth 1 -type d -name "${key}*" | sort | head -1)
    [ -n "$match" ] || die "No scenario matching '$key'. Try: ./lab.sh list"
    echo "$match"
}

token() { need_uv; uv run --quiet gateway/tokens.py "$@"; }

wait_ready() {
    # Probed over TCP from the HOST. During first boot the Postgres entrypoint
    # runs a temporary server with listen_addresses='' to apply schema/, and an
    # in-container check reports THAT server as ready while nothing is
    # listening on the published port yet.
    # Every endpoint a scenario actually talks to, not just the interesting
    # ones. nginx used to be missing here, and the cost was real: when it
    # failed to start, `up` said "Ready", and scenario 05 then reported that
    # its anomaly had not reproduced — sending you to read about gzip when the
    # answer was that a container was not running. A readiness check that
    # omits a dependency does not save time, it moves the confusion downstream.
    local i pg=0 rd=0 gw=0 ng=0 az=0 sg=0
    for i in $(seq 1 120); do
        psql "$DSN" -X -q -tAc 'SELECT 1' >/dev/null 2>&1 && pg=$(( pg + 1 )) || pg=0
        redis-cli -u "$RURL" ping >/dev/null 2>&1 && rd=$(( rd + 1 )) || rd=0
        curl -sf -o /dev/null --max-time 3 "$GW/healthz"                && gw=$(( gw + 1 )) || gw=0
        curl -sf -o /dev/null --max-time 3 "http://localhost:${NGINX_PORT}/healthz" && ng=$(( ng + 1 )) || ng=0
        curl -sf -o /dev/null --max-time 3 "http://localhost:${AUTHZ_PORT}/health"  && az=$(( az + 1 )) || az=0
        curl -sf -o /dev/null --max-time 3 "http://localhost:${API_SIGNED_PORT}/healthz" && sg=$(( sg + 1 )) || sg=0
        if [ "$pg" -ge 2 ] && [ "$rd" -ge 2 ] && [ "$gw" -ge 2 ] \
           && [ "$ng" -ge 2 ] && [ "$az" -ge 2 ] && [ "$sg" -ge 2 ]; then
            return 0
        fi
        sleep 1
    done
    printf '\033[31m%s\033[0m\n' "Not ready after 120s. Who answered:" >&2
    printf '  postgres:%s redis:%s traefik:%s nginx:%s authz:%s api-signed:%s\n' \
        "$pg" "$rd" "$gw" "$ng" "$az" "$sg" >&2
    die "Check the ones showing 0:  ./lab.sh logs <service>"
}

INSIGHT="http://localhost:${INSIGHT_PORT}"

insight_setup() {
    # Register this lab's Redis in the GUI, and get the consent screen out of
    # the way, so the first thing you see is the keyspace rather than a form.
    #
    # Deliberately NON-FATAL: RedisInsight is a convenience and no scenario
    # depends on it, so a failure here warns and moves on. A tool that makes
    # `up` fail because an optional viewer did not start is a tool people stop
    # using.
    local i
    for i in $(seq 1 45); do
        curl -sf -o /dev/null --max-time 3 "$INSIGHT/api/settings" && break
        sleep 1
    done
    if ! curl -sf -o /dev/null --max-time 3 "$INSIGHT/api/settings"; then
        printf '\033[33m%s\033[0m\n' "RedisInsight did not answer on ${INSIGHT}; skipping its setup."
        return 0
    fi

    # eula: required. analytics: DECLINED on purpose — this is a tool pointed at
    # a database, and its telemetry default is not a decision to make by
    # accident. The same reasoning as any dependency you add to a service.
    curl -sS -o /dev/null --max-time 5 -X PATCH "$INSIGHT/api/settings" \
        -H 'Content-Type: application/json' \
        -d '{"agreements":{"eula":true,"analytics":false,"notifications":false,"encryption":false}}' || true

    python3 - "$INSIGHT" gateway/redisinsight/databases.json <<'PYEOF' || true
import json, sys, urllib.request

base, path = sys.argv[1], sys.argv[2]


def api(method, endpoint, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}{endpoint}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode()
    return json.loads(body) if body else None


wanted = json.load(open(path))

# Match on (host, port, db), not on name: the name is what you see in the GUI
# and therefore the one field a user might change, while the triple is what
# actually identifies the connection. Matching on the mutable field is how an
# idempotent step turns into a duplicate-creating one.
def identity(db):
    return (db.get("host"), int(db.get("port", 6379)), int(db.get("db") or 0))


existing = {identity(db) for db in api("GET", "/api/databases") or []}
for db in wanted:
    if identity(db) in existing:
        print(f"  RedisInsight: {db['host']}:{db['port']} already registered")
        continue
    api("POST", "/api/databases", {
        "name": db["name"], "host": db["host"],
        "port": db["port"], "db": db.get("db", 0),
    })
    print(f"  RedisInsight: registered {db['name']} -> {db['host']}:{db['port']}")
PYEOF
}

usage() {
cat <<EOF
Realtime Gateway Lab

  ./lab.sh up                 build and start the whole stack
  ./lab.sh down               stop and delete everything
  ./lab.sh reset              re-apply schema + seed, delete every stream
  ./lab.sh status             containers, streams, decision cache
  ./lab.sh logs [svc] [-f]    container logs (api, authz, traefik, nginx, ...)

  ./lab.sh list               list scenarios
  ./lab.sh read <n>           print a scenario's README
  ./lab.sh run <n>            run the scenario's automated proof
  ./lab.sh run-all            every proof; non-zero if any claim stops holding

  ./lab.sh token <who> [ttl]  mint a JWT: alice | bob | carol | dan
  ./lab.sh as <who> <curl-args...>    curl the GATEWAY with that user's token
  ./lab.sh direct <curl-args...>      curl the SERVICE, bypassing the gateway
  ./lab.sh sse <who> [query]  open an SSE stream in this terminal
  ./lab.sh publish <dataset> [mode]   publish an event (fanout|shared|pointer)
  ./lab.sh revoke-token <jti> deny one token id at the gateway

  ./lab.sh streams            every stream and its contents
  ./lab.sh cli [args...]      redis-cli against the lab
  ./lab.sh psql [args...]     psql against the lab
  ./lab.sh dashboard          print the Traefik dashboard URL
  ./lab.sh insight            print the RedisInsight URL (and re-register the DB)

Endpoints:
  gateway    $GW            (Traefik: auth enforced here)
  dashboard  http://localhost:${DASHBOARD_PORT}/dashboard/
  nginx      http://localhost:${NGINX_PORT}/{buffered,streamed}/   (scenario 05)
  service    $DIRECT        (trusts gateway headers — published ON PURPOSE, scenario 03)
  service    http://localhost:${API_SIGNED_PORT}        (same code, requires a SIGNED assertion)
  authz      http://localhost:${AUTHZ_PORT}/auth
  insight    ${INSIGHT}                    (RedisInsight: browse the streams)
EOF
}

cmd="${1:-help}"; shift || true

case "$cmd" in

up)
    # LAB_BUILD_CA: path to an extra CA bundle, for machines behind a
    # TLS-inspecting proxy. Unset on a normal network, where it is a no-op.
    if [ -n "${LAB_BUILD_CA:-}" ]; then
        [ -r "$LAB_BUILD_CA" ] || die "LAB_BUILD_CA is set but $LAB_BUILD_CA is not readable"
        bold "Building with extra CA: $LAB_BUILD_CA"
        docker build --secret "id=ca,src=$LAB_BUILD_CA" -f app/Dockerfile -t realtime-gateway-lab-api .
        docker build --secret "id=ca,src=$LAB_BUILD_CA" -f gateway/authz/Dockerfile -t realtime-gateway-lab-authz .
        docker tag realtime-gateway-lab-api realtime-gateway-lab-api-signed
        docker compose up -d --no-build
    else
        docker compose up -d --build
    fi
    # Traefik and nginx read their config from bind mounts, and
    # `docker compose up -d` has no idea a mounted FILE changed — it only
    # recreates a container when the image or the service definition does. So
    # after a `git pull` that touched gateway/, the old process keeps serving
    # the old config, silently. Restarting them costs a second and removes an
    # entire class of "it works on my machine".
    docker compose restart traefik nginx >/dev/null 2>&1 || true
    wait_ready
    insight_setup
    bold "Ready."
    echo "  gateway    $GW"
    echo "  dashboard  http://localhost:${DASHBOARD_PORT}/dashboard/"
    echo "  service    $DIRECT   (bypasses the gateway — that is scenario 03)"
    echo "  insight    $INSIGHT   (streams, key tree, profiler)"
    echo
    echo "  ./lab.sh as alice /whoami"
    echo "  ./lab.sh list"
    ;;

down)
    docker compose down -v --remove-orphans
    ;;

reset)
    need_psql; need_rcli
    psql "$DSN" -q -v ON_ERROR_STOP=1 -f schema/01_schema.sql -f schema/02_seed.sql
    # SCAN + DEL rather than FLUSHDB: the decision cache and the streams share
    # this database, and a scenario that flushed both would silently reset the
    # gateway's state in the middle of a proof.
    # A read loop rather than `xargs -r`: -r is a GNU extension, and BSD xargs
    # (macOS) runs the command once with no arguments on empty input instead of
    # skipping it. Portable beats clever in a script people run on two OSes.
    redis-cli -u "$RURL" --scan --pattern 'feed:*' | while read -r k; do
        [ -n "$k" ] && redis-cli -u "$RURL" del "$k" >/dev/null
    done
    bold "Reset: schema + seed re-applied, all feed streams deleted."
    ;;

status)
    docker compose ps
    echo
    need_rcli
    bold "Streams"
    redis-cli -u "$RURL" --scan --pattern 'feed:*:stream:*' | sort | while read -r s; do
        printf '  %-40s %s entries\n' "$s" "$(redis-cli -u "$RURL" xlen "$s")"
    done
    echo
    bold "Gateway decision cache"
    redis-cli -u "$RURL" --scan --pattern 'authz:*' | sort | while read -r k; do
        printf '  %-52s ttl=%-5s %s\n' "$k" "$(redis-cli -u "$RURL" ttl "$k")" "$(redis-cli -u "$RURL" get "$k")"
    done
    ;;

logs)
    docker compose logs "$@"
    ;;

list)
    bold "Scenarios"
    for d in scenarios/*/; do
        n=$(basename "$d")
        t=$(grep -m1 '^# ' "$d/README.md" 2>/dev/null | sed 's/^# //')
        printf '  %-26s %s\n' "$n" "$t"
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
    failed=(); blocked=()
    for d in scenarios/*/; do
        n=$(basename "$d")
        [ -f "$d/run.py" ] || continue
        echo
        bold "════════ $n ════════"
        # Capture the code explicitly rather than relying on $? after an
        # AND-OR list: `set -e` makes that shape work by exemption rather than
        # by intent, and a script people read should not depend on knowing
        # which exemption applies.
        code=0
        uv run --quiet "$d/run.py" || code=$?
        # Exit 3 means the scenario could not run here (a precondition was not
        # met) — a different problem from a claim that stopped holding.
        # Reporting them as one number is how a broken environment gets read
        # as a broken system.
        case "$code" in
            0) ;;
            3) blocked+=("$n") ;;
            *) failed+=("$n") ;;
        esac
    done
    echo
    if [ ${#blocked[@]} -gt 0 ]; then
        printf '\033[33m%s\033[0m\n' "COULD NOT RUN: ${blocked[*]}  (environment, not a failed claim)"
    fi
    [ ${#failed[@]} -gt 0 ] && die "FAILED: ${failed[*]}"
    [ ${#blocked[@]} -gt 0 ] && exit 3
    bold "All scenarios reproduced and all fixes held."
    ;;

token)
    [ $# -ge 1 ] || die "usage: ./lab.sh token <alice|bob|carol|dan> [ttl-seconds]"
    token "$@"
    ;;

as)
    [ $# -ge 2 ] || die "usage: ./lab.sh as <who> <path-or-curl-args...>"
    who="$1"; shift
    t=$(token "$who")
    first="$1"; shift || true
    # A bare path is expanded against the gateway; anything else is passed to
    # curl untouched, so you can add -i, -N, -X, --data and so on.
    case "$first" in
        /*) set -- "$GW$first" "$@" ;;
        *)  set -- "$first" "$@" ;;
    esac
    exec curl -sS -H "Authorization: Bearer $t" "$@"
    ;;

direct)
    [ $# -ge 1 ] || die "usage: ./lab.sh direct <path-or-curl-args...>"
    first="$1"; shift || true
    case "$first" in
        /*) set -- "$DIRECT$first" "$@" ;;
        *)  set -- "$first" "$@" ;;
    esac
    exec curl -sS "$@"
    ;;

sse)
    [ $# -ge 1 ] || die "usage: ./lab.sh sse <who> [querystring]"
    who="$1"; shift
    q="${1:-}"
    t=$(token "$who")
    bold "Streaming $GW/feed${q:+?$q} as $who. Ctrl-C to stop."
    echo "In another terminal:  ./lab.sh publish 11"
    # -N disables curl's own output buffering. Without it you are debugging
    # curl's buffer rather than the gateway's — a genuinely common wrong turn.
    exec curl -sS -N -H "Authorization: Bearer $t" -H "Accept: text/event-stream" \
        "$GW/feed${q:+?$q}"
    ;;

publish)
    [ $# -ge 1 ] || die "usage: ./lab.sh publish <dataset-id> [fanout|shared|pointer]"
    ds="$1"; mode="${2:-fanout}"
    t=$(token alice)   # publishing requires the admin role
    curl -sS -X POST -H "Authorization: Bearer $t" \
        "$GW/events/${ds}?mode=${mode}" | python3 -m json.tool
    ;;

revoke-token)
    need_rcli
    [ $# -ge 1 ] || die "usage: ./lab.sh revoke-token <jti>"
    redis-cli -u "$RURL" set "authz:v1:revoked:$1" 1 >/dev/null
    bold "Token $1 denylisted."
    ;;

streams)
    need_rcli
    redis-cli -u "$RURL" --scan --pattern 'feed:*:stream:*' | sort | while read -r s; do
        bold "$s  ($(redis-cli -u "$RURL" xlen "$s") entries)"
        redis-cli -u "$RURL" xrange "$s" - + COUNT 20 | sed 's/^/    /'
    done
    ;;

cli)  need_rcli; exec redis-cli -u "$RURL" "$@" ;;
psql) need_psql; exec psql "$DSN" "$@" ;;

dashboard)
    echo "http://localhost:${DASHBOARD_PORT}/dashboard/"
    ;;

insight)
    insight_setup
    bold "$INSIGHT"
    cat <<EOF
  Worth opening, in this order:
    Browser              the key tree. ":" is rendered as a folder separator,
                         so feed:v1:stream:user:1 is a path you can navigate —
                         which is the payoff of the naming convention.
    a stream key         entries with their ids, which are also the SSE
                         cursors. Publish with './lab.sh publish 11' and watch
                         one mailbox grow and another not.
    Workbench            XINFO STREAM, XRANGE, XLEN by hand, with completion.
    Analysis Tools       memory by key pattern — how much RAM your fan-out
                         actually costs (scenario 02).
    Profiler             this is MONITOR. It costs real throughput; never
                         leave it running against production.
EOF
    ;;

help|--help|-h) usage ;;
*) usage; die "unknown command: $cmd" ;;
esac
