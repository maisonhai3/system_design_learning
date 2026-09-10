"""Owns stock. Calls nobody — end of the chain."""
from svc import serve, log

stock = {"keyboard": 5, "monitor": 1}


def reserve(body):
    have = stock.get(body["item"], 0)
    if have < body["qty"]:
        return 409, {"reason": f"only {have} {body['item']} left, wanted {body['qty']}"}
    stock[body["item"]] -= body["qty"]
    log("inventory", f"reserved {body['qty']} {body['item']} for {body['order_id']}")
    return 200, {"order_id": body["order_id"], "reserved": body["qty"]}


serve("inventory", 8003, {"/reserve": reserve})
