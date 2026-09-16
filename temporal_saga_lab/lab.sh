#!/usr/bin/env bash
# Temporal Saga Lab — one entry point for everything.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

ORDERS_PORT="${ORDERS_PORT:-8110}"
PAYMENTS_PORT="${PAYMENTS_PORT:-8111}"
INVENTORY_PORT="${INVENTORY_PORT:-8112}"
SHIPPING_PORT="${SHIPPING_PORT:-8113}"
TEMPORAL_PORT="${TEMPORAL_PORT:-7233}"
TEMPORAL_UI_PORT="${TEMPORAL_UI_PORT:-8233}"

export LAB_ORDERS_URL="http://localhost:${ORDERS_PORT}"
export LAB_PAYMENTS_URL="http://localhost:${PAYMENTS_PORT}"
export LAB_INVENTORY_URL="http://localhost:${INVENTORY_PORT}"
export LAB_SHIPPING_URL="http://localhost:${SHIPPING_PORT}"
export LAB_TEMPORAL="${LAB_TEMPORAL:-localhost:${TEMPORAL_PORT}}"
export LAB_STATE_DIR="${LAB_STATE_DIR:-.lab}"
export LAB_BIN_DIR="${LAB_BIN_DIR:-./bin}"

ORDERS="$LAB_ORDERS_URL"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

need_go()     { command -v go >/dev/null || die "go not found. Install Go 1.21+: https://go.dev/dl/"; }
need_docker() { docker info >/dev/null 2>&1 || die "the docker daemon is not reachable. Start Docker, or use: ./lab.sh local-up"; }
need_curl()   { command -v curl >/dev/null || die "curl not found."; }

# Everything that kills a process needs to know how processes are being run:
# docker containers, or plain binaries started by ./lab.sh local-up.
detect_runtime() {
    if [ -n "${LAB_RUNTIME:-}" ]; then echo "$LAB_RUNTIME"; return; fi
    if [ -f "$LAB_STATE_DIR/local" ]; then echo local; else echo docker; fi
}

scenario_dir() {
    local key="$1" match
    match=$(find scenarios -maxdepth 1 -type d -name "${key}*" | sort | head -1)
    [ -n "$match" ] || die "No scenario matching '$key'. Try: ./lab.sh list"
    echo "$match"
}

wait_ready() {
    # Probed over TCP from the HOST, because that is where the scenarios run.
    # A container reporting itself healthy tells you nothing about whether its
    # published port is reachable from here.
    local i ok=0
    for i in $(seq 1 120); do
        ok=1
        for p in "$ORDERS_PORT" "$PAYMENTS_PORT" "$INVENTORY_PORT" "$SHIPPING_PORT"; do
            curl -sf -o /dev/null --max-time 3 "http://localhost:${p}/healthz" || ok=0
        done
        # The workflow service answering gRPC is not enough: a scenario needs a
        # WORKER polling, or its order sits in a queue and the failure looks
        # like a timeout in the wrong place.
        if [ "$ok" = 1 ] && worker_ready; then return 0; fi
        sleep 1
    done
    printf '\033[31m%s\033[0m\n' "Not ready after 120s." >&2
    for p in "$ORDERS_PORT" "$PAYMENTS_PORT" "$INVENTORY_PORT" "$SHIPPING_PORT"; do
        printf '  localhost:%s -> %s\n' "$p" \
            "$(curl -sf -o /dev/null --max-time 2 "http://localhost:${p}/healthz" && echo ok || echo DOWN)" >&2
    done
    die "Check the ones showing DOWN:  ./lab.sh logs <service>"
}

# A worker is ready when an order actually completes. Cheaper probes (is the
# port open, is the container up) have all been wrong here at least once.
worker_ready() {
    local id resp phase i
    id="probe_$(date +%s%N)"
    resp=$(curl -sf --max-time 5 -X POST "$ORDERS/orders" -H 'content-type: application/json' \
        -d "{\"order_id\":\"$id\",\"customer\":\"probe\",\"amount_cents\":100}" 2>/dev/null) || return 1
    for i in $(seq 1 20); do
        phase=$(curl -sf --max-time 5 "$ORDERS/orders/$id" 2>/dev/null | sed -n 's/.*"phase":"\([A-Z_]*\)".*/\1/p')
        case "$phase" in
            COMPLETED) return 0 ;;
            FAILED|STUCK|REJECTED) return 1 ;;
        esac
        sleep 0.5
    done
    return 1
}

usage() {
    cat <<'EOF'
Temporal Saga Lab

  ./lab.sh up                 build and start everything (docker)
  ./lab.sh down               stop it and delete the volumes
  ./lab.sh local-up           same lab, no docker: host binaries + temporal CLI
  ./lab.sh local-down         stop the host binaries

  ./lab.sh test               the proofs that need no infrastructure at all
  ./lab.sh list               the scenarios
  ./lab.sh run 01             run one  (01..05, or a name fragment)
  ./lab.sh run-all            run them all; non-zero if any claim stops holding

  ./lab.sh ui                 print the Temporal Web UI url
  ./lab.sh ps | logs [svc]    what is running, and what it is saying
  ./lab.sh status             a one-screen summary of the lab

  ./lab.sh order [amount] [mode]     place an order (mode: temporal|naive)
  ./lab.sh order-status <id>         where is it
  ./lab.sh approve <id> [true|false] answer a waiting order
  ./lab.sh ledger <id>               what the customer was actually charged
  ./lab.sh chaos <svc> <mode> [times] [target]
  ./lab.sh reset                     clear the three service databases

  chaos services: payments inventory shipping
  chaos modes:    off | before_commit | after_commit | slow
  chaos targets:  charge refund reserve release ship   (default: any)
EOF
}

cmd="${1:-help}"
shift || true

case "$cmd" in

up)
    need_docker
    bold "building and starting six containers"
    docker compose up -d --build
    wait_ready
    bold "ready"
    dim  "  orders     http://localhost:${ORDERS_PORT}"
    dim  "  payments   http://localhost:${PAYMENTS_PORT}"
    dim  "  inventory  http://localhost:${INVENTORY_PORT}"
    dim  "  shipping   http://localhost:${SHIPPING_PORT}"
    dim  "  temporal   http://localhost:${TEMPORAL_UI_PORT}   <- open this one"
    echo
    dim  "next:  ./lab.sh run 01"
    ;;

down)
    need_docker
    docker compose down -v --remove-orphans
    ;;

local-up)
    # The lab without Docker. Needs Go and the temporal CLI:
    #   brew install temporal      (or: https://docs.temporal.io/cli)
    need_go
    command -v temporal >/dev/null || die "temporal CLI not found. Install it (https://docs.temporal.io/cli), or use ./lab.sh up"
    mkdir -p "$LAB_STATE_DIR" "$LAB_BIN_DIR" data
    bold "building"
    go build -o "$LAB_BIN_DIR/" ./cmd/...

    if ! curl -sf -o /dev/null --max-time 2 "http://localhost:${TEMPORAL_UI_PORT}" 2>/dev/null; then
        bold "starting the temporal dev server"
        nohup temporal server start-dev --ip 127.0.0.1 \
            --port "$TEMPORAL_PORT" --ui-port "$TEMPORAL_UI_PORT" \
            --db-filename "$LAB_STATE_DIR/temporal.db" --log-level warn \
            > "$LAB_STATE_DIR/temporal.log" 2>&1 &
        echo $! > "$LAB_STATE_DIR/temporal.pid"
        sleep 5
    fi

    bold "starting the services"
    for s in payments inventory shipping; do
        LAB_DB="data/$s.db" nohup "$LAB_BIN_DIR/$s" > "$LAB_STATE_DIR/$s.log" 2>&1 &
        echo $! > "$LAB_STATE_DIR/$s.pid"
    done
    for s in orders worker; do
        nohup "$LAB_BIN_DIR/$s" > "$LAB_STATE_DIR/$s.log" 2>&1 &
        echo $! > "$LAB_STATE_DIR/$s.pid"
    done
    touch "$LAB_STATE_DIR/local"
    wait_ready
    bold "ready (local mode)"
    dim  "  temporal UI  http://localhost:${TEMPORAL_UI_PORT}"
    dim  "  logs         $LAB_STATE_DIR/*.log"
    ;;

local-down)
    for f in "$LAB_STATE_DIR"/*.pid; do
        [ -e "$f" ] || continue
        kill "$(cat "$f")" 2>/dev/null || true
        rm -f "$f"
    done
    rm -f "$LAB_STATE_DIR/local"
    rm -rf data
    bold "stopped"
    ;;

test)
    need_go
    bold "the proofs that need nothing running"
    go test ./... "$@"
    ;;

list)
    bold "scenarios"
    for d in scenarios/*/; do
        name=$(basename "$d")
        title=$(sed -n '1s|^// Scenario ||p' "$d/main.go")
        printf '  \033[1m%-4s\033[0m %s\n' "${name%%_*}" "${title:-${name#*_}}"
    done
    ;;

run)
    need_go; need_curl
    [ $# -ge 1 ] || die "usage: ./lab.sh run <NN>"
    d=$(scenario_dir "$1"); shift
    LAB_RUNTIME="$(detect_runtime)" go run "./$d" "$@"
    ;;

run-all)
    need_go; need_curl
    failed=()
    for d in scenarios/*/; do
        if ! LAB_RUNTIME="$(detect_runtime)" go run "./${d%/}"; then
            failed+=("$(basename "$d")")
        fi
    done
    echo
    if [ ${#failed[@]} -eq 0 ]; then
        printf '\033[32m\033[1m ALL CLAIMS HOLD \033[0m\n'
    else
        printf '\033[31m\033[1m FAILED \033[0m %s\n' "${failed[*]}"
        exit 1
    fi
    ;;

ui)
    bold "http://localhost:${TEMPORAL_UI_PORT}"
    dim "Workflows -> pick one -> the event history IS the order. Every activity,"
    dim "every retry, every timer, in the sequence a replay will read back."
    ;;

ps)
    if [ "$(detect_runtime)" = local ]; then
        for f in "$LAB_STATE_DIR"/*.pid; do
            [ -e "$f" ] || continue
            n=$(basename "$f" .pid); p=$(cat "$f")
            printf '%-12s pid %-8s %s\n' "$n" "$p" "$(kill -0 "$p" 2>/dev/null && echo up || echo DOWN)"
        done
    else
        docker compose ps
    fi
    ;;

logs)
    if [ "$(detect_runtime)" = local ]; then
        tail -n 60 -f "$LAB_STATE_DIR/${1:-worker}.log"
    else
        docker compose logs -f --tail 60 "$@"
    fi
    ;;

status)
    need_curl
    bold "services"
    for s in orders:$ORDERS_PORT payments:$PAYMENTS_PORT inventory:$INVENTORY_PORT shipping:$SHIPPING_PORT; do
        printf '  %-12s %s\n' "${s%%:*}" \
            "$(curl -sf -o /dev/null --max-time 2 "http://localhost:${s##*:}/healthz" && echo up || echo DOWN)"
    done
    echo
    bold "stock"
    curl -sf "$LAB_PAYMENTS_URL/healthz" >/dev/null && curl -s "$LAB_INVENTORY_URL/stock" && echo
    echo
    bold "chaos armed"
    for s in payments:$PAYMENTS_PORT inventory:$INVENTORY_PORT shipping:$SHIPPING_PORT; do
        printf '  %-12s %s\n' "${s%%:*}" "$(curl -s --max-time 2 "http://localhost:${s##*:}/_chaos" || echo '-')"
    done
    ;;

order)
    need_curl
    amount="${1:-1999}"; mode="${2:-temporal}"
    curl -s -X POST "$ORDERS/orders" -H 'content-type: application/json' \
        -d "{\"customer\":\"you\",\"amount_cents\":${amount},\"mode\":\"${mode}\",\"pack_seconds\":0}"
    echo
    ;;

order-status)
    need_curl
    [ $# -ge 1 ] || die "usage: ./lab.sh order-status <order-id>"
    curl -s "$ORDERS/orders/$1"; echo
    ;;

approve)
    need_curl
    [ $# -ge 1 ] || die "usage: ./lab.sh approve <order-id> [true|false]"
    curl -s -X POST "$ORDERS/orders/$1/approve" -H 'content-type: application/json' \
        -d "{\"approved\":${2:-true},\"by\":\"cli\"}"
    echo
    ;;

ledger)
    need_curl
    [ $# -ge 1 ] || die "usage: ./lab.sh ledger <order-id>"
    curl -s "$LAB_PAYMENTS_URL/ledger?order_id=$1"; echo
    ;;

chaos)
    need_curl
    [ $# -ge 2 ] || die "usage: ./lab.sh chaos <payments|inventory|shipping> <mode> [times] [target]"
    case "$1" in
        payments)  base="$LAB_PAYMENTS_URL" ;;
        inventory) base="$LAB_INVENTORY_URL" ;;
        shipping)  base="$LAB_SHIPPING_URL" ;;
        *) die "unknown service: $1" ;;
    esac
    curl -s -X POST "$base/_chaos" -H 'content-type: application/json' \
        -d "{\"mode\":\"$2\",\"times\":${3:-1},\"target\":\"${4:-}\"}"
    echo
    ;;

reset)
    need_curl
    for base in "$LAB_PAYMENTS_URL" "$LAB_INVENTORY_URL" "$LAB_SHIPPING_URL"; do
        curl -s -X DELETE "$base/_all" >/dev/null
    done
    bold "cleared"
    ;;

help|--help|-h) usage ;;
*) usage; exit 1 ;;
esac
