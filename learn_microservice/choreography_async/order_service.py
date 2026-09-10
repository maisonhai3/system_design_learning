"""Owns orders. Never calls payment or inventory — it emits a fact and later
reacts to facts others emit."""
import asyncio
from bus import Bus, log

bus = Bus("order")
orders = {}     # this service's private state


@bus.on("orders.place")             # a COMMAND from the edge (would be an HTTP POST in real life)
async def place(cmd):
    o = {k: cmd[k] for k in ("order_id", "item", "qty", "amount")}
    orders[o["order_id"]] = {**o, "status": "PENDING"}
    await bus.publish("OrderPlaced", **o)


@bus.on("StockReserved")
async def confirm(ev):
    orders[ev["order_id"]]["status"] = "CONFIRMED"
    log("order", f"✔ {ev['order_id']} CONFIRMED")


@bus.on("PaymentFailed")
@bus.on("PaymentRefunded")
async def cancel(ev):
    orders[ev["order_id"]]["status"] = "CANCELLED"
    log("order", f"✘ {ev['order_id']} CANCELLED — {ev['reason']}")


asyncio.run(bus.run())
