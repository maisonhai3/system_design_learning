#!/usr/bin/env bash
# 04 — at-least-once delivery, watched rather than read about.
source "$(dirname "$0")/../_lib.sh"
need_kafka
fresh_log

ledger() { # ledger — print the payment ledger, one movement per line
  if [ -s state/ledger-1.log ]; then sed 's/^/     /' state/ledger-1.log
  else echo "     (empty)"; fi
}

step "PART A — no idempotency guard"
svc projector projector
svc inventory inventory
svc gateway   gateway
wait_for_http "$GATEWAY/healthz"
svc paymentA payment -instance 1 -crash-after 1
sleep 4

step "one order, and a payment service that dies at the worst moment"
note "it will charge the card, then exit BEFORE committing the offset"
id=$(order widget 2 ada)
sleep 5

step "the ledger after the crash — one charge, which is correct"
ledger

step "restart payment; Kafka redelivers the record it never saw committed"
svc paymentB payment -instance 1
sleep 8

step "the ledger now"
ledger
note "two movements for one \$40 order. Nothing was lost — something was repeated."
note "This is at-least-once, and it is the default because the alternative is loss."
curl -sS "$PROJECTOR/orders/$id"

step "PART B — identical crash, guard switched on"
stop_all
fresh_log
svc projector2 projector
svc inventory2 inventory
svc gateway2   gateway
wait_for_http "$GATEWAY/healthz"
svc paymentC payment -instance 1 -idempotent -crash-after 1
sleep 4

id=$(order widget 2 ada)
sleep 5
step "ledger after the crash"
ledger

step "restart it, guard still on"
svc paymentD payment -instance 1 -idempotent
sleep 8

step "the ledger now"
ledger
note "the record was delivered twice and applied once."
note "Note what did NOT change: Kafka still redelivered it. You cannot switch"
note "that off — you can only make the second application a no-op."

step "done — read scenarios/04_duplicates/README.md"
