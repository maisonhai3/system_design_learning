# Choreography in 9 files

Five processes. No service knows any other service exists — they only know topics.

```
client ──orders.place──▶ order ──OrderPlaced──▶ payment ──PaymentCompleted──▶ inventory
                           ▲                       │  │                          │
                           │                       │  └─PaymentFailed────────────┤
                           │                       │                             │
                           ├──StockReserved────────┼─────────────────────────────┤
                           └──PaymentRefunded◀─────┘◀──StockRejected─────────────┘
                                          (audit hears everything)
```

Run: `./run.sh`

Three orders, three paths:
- A1 — paid, reserved, CONFIRMED
- B2 — payment declines, CANCELLED
- C3 — paid, stock rejects, payment refunds itself, CANCELLED (a saga with no coordinator)

Files
- broker.py   dumb fan-out; the only shared infrastructure
- bus.py      subscribe/publish helper every service imports
- *_service.py  each owns one thing and one dict of private state
- audit_service.py  your eyes; the only process that sees the whole flow
- client.py   the outside world
