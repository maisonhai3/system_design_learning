"""The outside world. Fires three orders that take three different paths."""
import asyncio
from bus import Bus

ORDERS = [
    dict(order_id="A1", item="keyboard", qty=2, amount=120),   # happy path
    dict(order_id="B2", item="monitor",  qty=3, amount=900),   # payment declines
    dict(order_id="C3", item="monitor",  qty=2, amount=400),   # paid, then out of stock -> refund
]


async def main():
    bus = Bus("client")
    await bus.connect()
    for o in ORDERS:
        print(f"\n────────── order {o['order_id']} ──────────", flush=True)
        await bus.publish("orders.place", **o)
        await asyncio.sleep(1.2)

asyncio.run(main())
