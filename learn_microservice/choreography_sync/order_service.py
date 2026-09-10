"""Owns orders. Knows exactly one other service: payment. Not inventory."""
from svc import serve, call, log, Downstream

orders = {}
PAYMENT = "http://127.0.0.1:8002/charge"


def place(body):
    o = {**body, "status": "PENDING"}
    orders[o["order_id"]] = o
    try:
        call("order", PAYMENT, **body)          # blocks until payment (and whoever payment calls) is done
    except Downstream as d:
        o["status"] = "CANCELLED"
        log("order", f"✘ {o['order_id']} CANCELLED — {d.body}")
        return 409, {"order_id": o["order_id"], "status": "CANCELLED", "reason": d.body}
    o["status"] = "CONFIRMED"
    log("order", f"✔ {o['order_id']} CONFIRMED")
    return 201, {"order_id": o["order_id"], "status": "CONFIRMED"}


serve("order", 8001, {"/orders": place})
