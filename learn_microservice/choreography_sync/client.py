"""The outside world. Talks to ONE url and waits for a definitive answer."""
import sys, time
from svc import call, Downstream

ORDERS = {
    "A1": dict(order_id="A1", item="keyboard", qty=2, amount=120),   # happy path
    "B2": dict(order_id="B2", item="monitor",  qty=3, amount=900),   # payment declines
    "C3": dict(order_id="C3", item="monitor",  qty=2, amount=400),   # paid, out of stock -> refund
    "D4": dict(order_id="D4", item="keyboard", qty=1, amount=60),    # fine order... run with inventory dead
}

for oid in sys.argv[1:]:
    print(f"\n────────── order {oid} ──────────", flush=True)
    t0 = time.perf_counter()
    try:
        result = call("client", "http://127.0.0.1:8001/orders", **ORDERS[oid])
    except Downstream as d:
        result = d.body
    print(f"   client got answer in {1000*(time.perf_counter()-t0):.0f} ms: {result}", flush=True)
