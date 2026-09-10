"""Owns stock. Reserves only after money is in hand."""
import asyncio
from bus import Bus

bus = Bus("inventory")
stock = {"keyboard": 5, "monitor": 1}


@bus.on("PaymentCompleted")
async def reserve(ev):
    have = stock.get(ev["item"], 0)
    if have >= ev["qty"]:
        stock[ev["item"]] -= ev["qty"]
        await bus.publish("StockReserved", order_id=ev["order_id"],
                          item=ev["item"], qty=ev["qty"])
    else:
        await bus.publish("StockRejected", order_id=ev["order_id"],
                          reason=f"only {have} {ev['item']} left, wanted {ev['qty']}")


asyncio.run(bus.run())
