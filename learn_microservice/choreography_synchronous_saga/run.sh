#!/usr/bin/env bash
cd "$(dirname "$0")"
trap 'kill 0' EXIT
python3 -u vm_service.py &
python3 -u disk_service.py &
python3 -u ip_service.py &
python3 -u dns_service.py &
sleep 0.6
python3 -u client.py alpha beta STATE
