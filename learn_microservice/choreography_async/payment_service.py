"""Owns charges. Charges when an order is placed; refunds when stock is rejected."""
import asyncio
from bus import Bus

bus = Bus("payment")
charges = {}    # order_id -> amount


@bus.on("OrderPlaced")
async def charge(ev):
    await asyncio.sleep(0.2)                     # pretend to call the card processor
    if ev["amount"] > 500:
        await bus.publish("PaymentFailed", order_id=ev["order_id"],
                          reason=f"card declined for {ev['amount']}")
        return
    charges[ev["order_id"]] = ev["amount"]
    await bus.publish("PaymentCompleted", order_id=ev["order_id"],
                      item=ev["item"], qty=ev["qty"])


@bus.on("StockRejected")                         # compensating action
async def refund(ev):
    amount = charges.pop(ev["order_id"])
    await asyncio.sleep(0.2)
    await bus.publish("PaymentRefunded", order_id=ev["order_id"],
                      reason=f"refunded {amount}: {ev['reason']}")


asyncio.run(bus.run())
