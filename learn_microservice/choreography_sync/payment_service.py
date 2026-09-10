"""Owns charges. Knows exactly one other service: inventory."""
from svc import serve, call, log, Downstream

charges = {}
INVENTORY = "http://127.0.0.1:8003/reserve"


def charge(body):
    import time; time.sleep(0.2)                # pretend to call the card processor
    if body["amount"] > 500:
        return 402, {"reason": f"card declined for {body['amount']}"}
    charges[body["order_id"]] = body["amount"]
    log("payment", f"charged {body['amount']} for {body['order_id']}")
    try:
        call("payment", INVENTORY, order_id=body["order_id"], item=body["item"], qty=body["qty"])
    except Downstream as d:                     # compensate: money must not stay taken
        amount = charges.pop(body["order_id"])
        time.sleep(0.2)
        log("payment", f"refunded {amount} for {body['order_id']}")
        return 409, {"reason": f"refunded {amount}", "cause": d.body}
    return 200, {"order_id": body["order_id"], "charged": body["amount"], "reserved": True}


serve("payment", 8002, {"/charge": charge})
