#!/usr/bin/env bash
# 02 — a backlog, three consumers in one group, and a rebalance.
source "$(dirname "$0")/../_lib.sh"
need_kafka
fresh_log

step "starting everything except a second payment service"
svc projector projector
svc inventory inventory
svc gateway   gateway
svc payment1  payment -instance 1
wait_for_http "$GATEWAY/healthz"
sleep 3

# Post enough orders that the group commits an offset on every partition,
# otherwise the lag table below only shows the partitions it happened to touch.
for _ in $(seq 1 8); do order bolt 1 ada >/dev/null; done
sleep 3

step "now stop the only payment service — orders keep being accepted"
stop payment1
sleep 2

step "30 orders arrive with nobody to charge them"
"$LAB/bin/labctl" -gateway "$GATEWAY" -n 30 -item bolt flood

step "that backlog has a name: consumer lag"
note "END is where the log ends, COMMITTED is how far payment-svc got"
note "the gateway never slowed down — the backlog is a queue of work, not an outage"
"$LAB/bin/labctl" -brokers "$BROKERS" lag | grep -E "GROUP|payment-svc"

step "start THREE payment services, all in the same consumer group"
note "same group = split the partitions between us; watch them each take one"
svc payment1 payment -instance 1
svc payment2 payment -instance 2
svc payment3 payment -instance 3
sleep 12

step "lag drained"
"$LAB/bin/labctl" -brokers "$BROKERS" lag | grep -E "GROUP|payment-svc"

step "who handled which partition"
for i in 1 2 3; do
  parts=$(grep -oE 'orders/[0-9]+' "logs/payment$i.log" 2>/dev/null | sort -u | tr '\n' ' ')
  printf '   payment#%s consumed: %s\n' "$i" "${parts:-(nothing — there are only 3 partitions to go round)}"
done
note "3 partitions, 3 consumers, one each. A 4th would have sat idle:"
note "partitions are the unit of parallelism, so they cap how far you can scale out."

step "kill payment#2 and post more orders"
stop payment2
sleep 10
# Clear the two survivors' logs so the summary below shows what they consumed
# AFTER the rebalance, not everything they have ever touched. Without this the
# cumulative view shows two consumers on the same partition, which is exactly
# the thing that never happens within a group.
: > logs/payment1.log
: > logs/payment3.log
for _ in $(seq 1 6); do order bolt 1 ada >/dev/null; done
sleep 6

step "payment#2's partition was reassigned — nothing was lost"
note "partitions consumed SINCE the kill, so you can see the new split:"
for i in 1 3; do
  parts=$(grep -oE 'orders/[0-9]+' "logs/payment$i.log" | sort -u | tr '\n' ' ')
  printf '   payment#%s now consumes: %s\n' "$i" "${parts:-(idle this round)}"
done
note "two consumers, three partitions — one of them holds two"
"$LAB/bin/labctl" -brokers "$BROKERS" lag | grep -E "GROUP|payment-svc"

step "done — read scenarios/02_scale_out/README.md"
