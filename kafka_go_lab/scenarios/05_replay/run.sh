#!/usr/bin/env bash
# 05 — the log is the source of truth; everything else is a cache of it.
source "$(dirname "$0")/../_lib.sh"
need_kafka
fresh_log

step "starting the four services and placing the usual three orders"
svc projector projector
svc payment   payment
svc inventory inventory
svc gateway   gateway
wait_for_http "$GATEWAY/healthz"
wait_for_http "$PROJECTOR/healthz"
sleep 2
order widget 2 ada       >/dev/null
order gizmo 4 grace      >/dev/null
order doohickey 1 linus  >/dev/null
sleep 4

step "the read model the running projector has built"
curl -sS "$PROJECTOR/stats"

step "now a SECOND projector, new consumer group, same log"
note "a group it has never used has no committed offsets, so it starts at 0"
note "and reads the entire history — nobody had to keep a backup for this"
replay_group="replay-$(date +%s)"
svc projector_replay projector -group "$replay_group" -addr :8082
wait_for_http "http://localhost:8082/healthz"
sleep 6

step "what the replayed projector rebuilt, from nothing but the log"
curl -sS "http://localhost:8082/stats"

step "do the two agree?"
live=$(curl -sS "$PROJECTOR/stats" | tr -d ' \n')
replayed=$(curl -sS "http://localhost:8082/stats" | tr -d ' \n')
if [ "$live" = "$replayed" ]; then
  printf '   \033[32mIDENTICAL\033[0m — the log was enough to reconstruct the state\n'
else
  printf '   \033[31mDIFFERENT\033[0m — see the README: the fold depends on read order\n'
fi
note "this is not free. Getting here needed a fold that does not care what"
note "order the events arrive in — see stage()/advance() in cmd/projector."

step "and the original group never noticed"
note "reading is not consuming: a new reader costs the old one nothing"
"$LAB/bin/labctl" -brokers "$BROKERS" lag | grep -E "GROUP|projector-svc"

step "done — read scenarios/05_replay/README.md"
