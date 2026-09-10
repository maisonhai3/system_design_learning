#!/usr/bin/env bash
cd "$(dirname "$0")"
trap 'kill 0' EXIT
python3 -u order_service.py &
python3 -u payment_service.py &
python3 -u inventory_service.py & INV=$!
sleep 0.6
python3 -u client.py A1 B2 C3

echo; echo "══════════ killing inventory, then placing D4 ══════════"
kill $INV; sleep 0.3
python3 -u client.py D4
