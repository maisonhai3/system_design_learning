#!/usr/bin/env bash
# Starts broker + 4 services as separate processes, fires 3 orders, tears down.
cd "$(dirname "$0")"
trap 'kill 0' EXIT
python3 -u broker.py &
sleep 0.3
for s in order payment inventory audit; do python3 -u ${s}_service.py & done
sleep 0.5
python3 -u client.py
sleep 1
