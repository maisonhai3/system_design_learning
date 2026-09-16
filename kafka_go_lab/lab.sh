#!/usr/bin/env bash
# lab.sh — the one entry point. Run it with no arguments to see the verbs.
#
# Kafka runs in Docker; the Go services run on your machine. That split is on
# purpose: you will spend this lab killing services, starting three copies of
# one, and restarting them with different flags, and `go run` makes that a
# keystroke instead of an image rebuild.
set -euo pipefail
cd "$(dirname "$0")"

BROKERS="${KAFKA_BROKERS:-localhost:9092}"
GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
PROJECTOR="${PROJECTOR_URL:-http://localhost:8081}"
LOGS=logs

build() { mkdir -p bin && go build -o bin/ ./cmd/...; }

wait_for_kafka() {
  echo "waiting for Kafka on $BROKERS ..."
  for _ in $(seq 1 60); do
    if bin/labctl -brokers "$BROKERS" topics >/dev/null 2>&1; then
      echo "Kafka is up."
      return 0
    fi
    sleep 1
  done
  echo "Kafka did not come up on $BROKERS" >&2
  return 1
}

# start_services runs the four services in the background, each with its own
# log file, and records the PIDs so stop() can find them again.
start_services() {
  mkdir -p "$LOGS"
  : > "$LOGS/pids"
  for svc in projector payment inventory gateway; do
    bin/$svc -brokers "$BROKERS" >"$LOGS/$svc.log" 2>&1 &
    echo $! >> "$LOGS/pids"
  done
  sleep 2
  echo "services started; logs in $LOGS/"
}

stop_services() {
  [ -f "$LOGS/pids" ] || return 0
  while read -r pid; do kill "$pid" 2>/dev/null || true; done < "$LOGS/pids"
  rm -f "$LOGS/pids"
}

order() { # order <item> [qty] [customer]
  curl -sS -X POST "$GATEWAY/orders" -H 'Content-Type: application/json' \
    -d "{\"customer\":\"${3:-ada}\",\"item\":\"$1\",\"qty\":${2:-1}}"
  echo
}

case "${1:-}" in
  up)
    docker compose up -d
    build
    wait_for_kafka
    bin/labctl -brokers "$BROKERS" topics
    echo
    echo "Kafka is ready. Next: ./lab.sh run   (or ./lab.sh scenario 01)"
    ;;
  down)
    stop_services
    docker compose down -v
    ;;
  build) build ;;

  # run: all four services interleaved into this terminal. Ctrl-C stops them.
  run)
    build
    trap 'kill 0' EXIT INT TERM
    bin/projector -brokers "$BROKERS" &
    bin/payment   -brokers "$BROKERS" &
    bin/inventory -brokers "$BROKERS" &
    bin/gateway   -brokers "$BROKERS" &
    echo "--- four services running. In another terminal: ./lab.sh demo ---"
    wait
    ;;

  # demo: the three orders this lab is built around, one per outcome.
  demo)
    echo '1. widget x2 ($40)     -> paid, reserved, CONFIRMED'
    order widget 2 ada
    echo '2. gizmo x4 ($600)     -> over the limit, DECLINED'
    order gizmo 4 grace
    echo '3. doohickey x1 ($300) -> paid, then out of stock, refunded, CANCELLED'
    order doohickey 1 linus
    echo
    echo "watch the service logs, then: ./lab.sh status"
    ;;

  order) shift; order "$@" ;;
  status) curl -sS "$PROJECTOR/orders"; echo ;;
  stats)  curl -sS "$PROJECTOR/stats"; echo ;;
  reset)
    build
    stop_services
    bin/labctl -brokers "$BROKERS" reset
    rm -rf state logs
    echo "clean log, clean offsets, clean ledger"
    ;;
  lag)    build; bin/labctl -brokers "$BROKERS" lag ;;
  dump)   build; bin/labctl -brokers "$BROKERS" -topic "${2:-orders}" dump ;;
  flood)  build; bin/labctl -gateway "$GATEWAY" -n "${2:-50}" -item "${3:-widget}" flood ;;

  scenario)
    [ -n "${2:-}" ] || { echo "usage: ./lab.sh scenario <number>" >&2; exit 2; }
    dir=$(ls -d scenarios/"$2"_* 2>/dev/null | head -1)
    [ -n "$dir" ] || { echo "no scenario $2" >&2; exit 2; }
    build
    exec "$dir/run.sh"
    ;;

  start-services) build; start_services ;;
  stop-services)  stop_services ;;

  *)
    cat <<'USAGE'
./lab.sh <verb>

  up                 start Kafka in Docker, build, create topics
  run                run all four services in this terminal (Ctrl-C to stop)
  demo               post the three canonical orders
  status             the projector's read model
  stats              counts by status, and records per partition
  lag                how far behind each consumer group is
  dump [topic]       print a topic's raw contents, oldest first
  flood [n] [item]   post a burst of orders
  reset              empty the topics, offsets and ledger, for a clean run
  scenario <n>       run scenario n (01..05); see scenarios/
  down               stop everything and delete the Kafka volume

  Point at a broker that is not in Docker with:  KAFKA_BROKERS=host:9092 ./lab.sh run
USAGE
    ;;
esac
