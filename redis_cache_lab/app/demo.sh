#!/usr/bin/env bash
# Drive the scenario 03 race against the running app, over HTTP, by hand.
#
# The lab's run.py forces this interleaving with a thread latch. Here we widen
# the window instead — ?stall_ms pauses the reader between its Postgres read
# and its Redis write — so you can watch two ordinary curl requests do it.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

B="http://127.0.0.1:${APP_PORT:-8001}"
DSN="${LAB_DSN:-postgresql://lab:lab@localhost:5436/lab}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

curl -sf "$B/debug/stats" >/dev/null || {
    printf '\033[31m%s\033[0m\n' "The app is not running. Start it with: ./lab.sh app" >&2
    exit 1
}

pg_role() { psql "$DSN" -X -tAc "SELECT role FROM users WHERE id = 1"; }
cached()  { curl -s "$B/debug/keys" | python3 -c "
import json,sys
for k in json.load(sys.stdin):
    if k['key'].startswith('identity:'):
        print(f\"  {k['key']}  ttl={k['ttl']}  {k['value']}\")
" ; }

reset() {
    psql "$DSN" -X -q -c "UPDATE users SET role = 'guest' WHERE id = 1" >/dev/null
    curl -s -X POST "$B/debug/flush" >/dev/null
}

# ---------------------------------------------------------------------------
bold "═══ Part 1: correct ordering, poisoned anyway (scenario 03) ═══"
reset
echo
dim "t=0.0s  reader: GET /users/1?stall_ms=1500"
dim "        it will miss, read 'guest' from Postgres, then stall before SET"
curl -s "$B/users/1?stall_ms=1500" > /tmp/reader.json &
READER=$!

sleep 0.4
echo
dim "t=0.4s  writer: PUT /users/1/role?role=admin&mode=del_after_commit"
dim "        the CORRECT ordering — commit, then delete"
curl -s -X PUT "$B/users/1/role?role=admin&mode=del_after_commit" >/dev/null

echo
dim "t=0.4s  the DEL just ran. There was nothing to delete: the reader"
dim "        has not written yet. The delete achieved nothing and said so"
dim "        by returning 0, which nobody checked."

wait $READER
echo
dim "t=1.9s  reader wakes and SETs the value it read at t=0.0"
echo
bold "  Postgres:  $(pg_role)"
bold "  Redis:"
cached
echo
curl -s -D/tmp/h -o/dev/null "$B/users/1"
bold "  Next GET /users/1 → $(grep -i '^x-cache:' /tmp/h | tr -d '\r')"
dim "  A hit never re-reads Postgres, so this is now self-sustaining."

# ---------------------------------------------------------------------------
echo
bold "═══ Part 2: same race, version-keyed (the fix) ═══"
reset
echo
dim "t=0.0s  reader: GET /users/1?versioned=true&stall_ms=1500"
curl -s "$B/users/1?versioned=true&stall_ms=1500" > /tmp/reader.json &
READER=$!
sleep 0.4
dim "t=0.4s  writer: PUT /users/1/role?role=admin&mode=versioned"
curl -s -X PUT "$B/users/1/role?role=admin&mode=versioned" >/dev/null
wait $READER
echo
bold "  Postgres:  $(pg_role)"
bold "  Redis:"
cached
echo
bold "  Next read: $(curl -s "$B/users/1?versioned=true" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["user"]["role"], "(cache_hit=%s)" % d["cache_hit"])')"
echo
dim "  Two value keys above, and the pointer names the newer one. The stale"
dim "  reader still wrote — to the older @version, which nothing will look up"
dim "  again. Garbage, not corruption, and its TTL collects it."
echo
bold "Now go read: ./lab.sh read 03"
