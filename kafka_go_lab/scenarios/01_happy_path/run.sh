#!/usr/bin/env bash
# 01 — one order, four services, and a log you can read.
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

step "three orders, three different endings"
note "widget x2 (\$40)      — affordable and in stock"
order widget 2 ada >/dev/null
note "gizmo x4 (\$600)      — over the payment limit"
order gizmo 4 grace >/dev/null
note "doohickey x1 (\$300)  — paid for, then found to be out of stock"
order doohickey 1 linus >/dev/null
sleep 3

step "what the read model says happened"
curl -sS "$PROJECTOR/orders"

step "what is actually on the orders topic"
note "nothing consumed these records away — they are still there, in order"
"$LAB/bin/labctl" -brokers "$BROKERS" -topic orders dump

step "and on the payments topic"
"$LAB/bin/labctl" -brokers "$BROKERS" -topic payments dump

step "done — read scenarios/01_happy_path/README.md for what to notice"
