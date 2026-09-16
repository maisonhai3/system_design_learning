#!/usr/bin/env bash
# 03 — what Kafka orders, and what it very much does not.
source "$(dirname "$0")/../_lib.sh"
need_kafka
fresh_log

step "starting the four services"
svc projector projector
svc payment   payment
svc inventory inventory
svc gateway   gateway
wait_for_http "$GATEWAY/healthz"
wait_for_http "$PROJECTOR/healthz"
sleep 2

step "40 orders, each with its own key"
"$LAB/bin/labctl" -gateway "$GATEWAY" -n 40 -item bolt flood
sleep 4

step "how the keys spread across partitions"
note "partition = hash(key) % 3, so the spread is even but not exactly equal"
curl -sS "$PROJECTOR/stats"

step "a key is never split across partitions"
"$LAB/bin/labctl" -brokers "$BROKERS" -topic orders dump \
  | awk '/^=== /{ p=$4; sub(/:/,"",p) } /key=/{ k=$2; sub(/key=/,"",k); print k, p }' \
  | sort -u > logs/keymap.txt
split_keys=$(awk '{c[$1]++} END{ n=0; for (k in c) if (c[k] > 1) n++; print n+0 }' logs/keymap.txt)
printf '   %s distinct keys, %s of them on more than one partition\n' \
  "$(awk '{print $1}' logs/keymap.txt | sort -u | wc -l | tr -d ' ')" "$split_keys"
note "that is the whole guarantee: same key -> same partition -> ordered"

step "now one order whose story has two chapters"
id=$(order doohickey 1 linus)
sleep 4
note "$id was paid for, rejected by inventory, then refunded"
note "both chapters are on one partition, in the order they happened:"
"$LAB/bin/labctl" -brokers "$BROKERS" -topic payments dump | grep -E "=== payments|key=$id " || true

step "the trap: there is no global order"
note "each partition has its own offset counter, all starting at 0."
note "offset 5 on partition 0 and offset 5 on partition 2 say nothing about"
note "which happened first. If you need to compare across partitions, you need"
note "a timestamp or a sequence number that you put there yourself."
"$LAB/bin/labctl" -brokers "$BROKERS" lag | tail -4

step "done — read scenarios/03_ordering/README.md"
