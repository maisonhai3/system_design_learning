# Shared helpers for the scenario scripts. Sourced, not run.
set -euo pipefail

LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$LAB"

BROKERS="${KAFKA_BROKERS:-localhost:9092}"
GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
PROJECTOR="${PROJECTOR_URL:-http://localhost:8081}"

mkdir -p logs

declare -A PID=()

# Kill everything this scenario started. Services are launched by absolute
# path so the pkill fallback cannot match anything that is not ours.
cleanup() {
  for label in "${!PID[@]}"; do kill "${PID[$label]}" 2>/dev/null || true; done
  pkill -f "$LAB/bin/" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

step() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
note() { printf '   \033[2m%s\033[0m\n' "$*"; }

need_kafka() {
  "$LAB/bin/labctl" -brokers "$BROKERS" topics >/dev/null 2>&1 && return 0
  echo "Kafka is not reachable at $BROKERS. Start it with ./lab.sh up" >&2
  exit 1
}

# fresh_log empties the topics, the committed offsets and the payment ledger,
# so a scenario tells the same story every time it runs. Without it, the second
# run replays on top of the first one's records and the order IDs collide.
fresh_log() {
  "$LAB/bin/labctl" -brokers "$BROKERS" reset >/dev/null
  rm -rf "$LAB/state"
}

# svc <label> <binary> [flags...] — run a service, tee its output to
# logs/<label>.log, and remember its PID under <label> so a scenario can kill
# one instance without touching the others.
svc() {
  local label="$1" bin="$2"; shift 2
  : > "logs/$label.log"
  "$LAB/bin/$bin" -brokers "$BROKERS" "$@" > >(tee -a "logs/$label.log") 2>&1 &
  PID[$label]=$!
}

stop() { # stop <label>
  local label="$1"
  kill "${PID[$label]}" 2>/dev/null || true
  unset 'PID[$label]'
}

# order <item> [qty] [customer] — prints the order id the gateway assigned, so
# a scenario can follow one order through the log.
stop_all() {
  for label in "${!PID[@]}"; do kill "${PID[$label]}" 2>/dev/null || true; unset 'PID[$label]'; done
  sleep 2
}

order() {
  curl -sS -X POST "$GATEWAY/orders" -H 'Content-Type: application/json' \
    -d "{\"customer\":\"${3:-ada}\",\"item\":\"$1\",\"qty\":${2:-1}}" \
    | grep -o '"order_id":"[^"]*"' | cut -d'"' -f4
}

# wait_for_http <url> — the services take a moment to join their groups.
wait_for_http() {
  for _ in $(seq 1 40); do
    curl -sSf "$1" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "timed out waiting for $1" >&2
  return 1
}
