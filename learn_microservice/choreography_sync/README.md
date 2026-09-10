# Choreography with synchronous calls

Same domain as the event version, same three orders. No mediator: order knows
only payment's URL, payment knows only inventory's URL, inventory knows nobody.

```
client ─POST /orders─▶ order ─POST /charge─▶ payment ─POST /reserve─▶ inventory
       ◀── 201/409 ────      ◀── 200/402/409 ──        ◀── 200/409 ────
```

Run: `bash run.sh`

Then the script kills inventory and places D4. Watch payment charge, get no
answer, refund, and the client receive a 409 — the chain fails as a unit.
