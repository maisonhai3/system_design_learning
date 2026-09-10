from svc import serve, log

registered = {}


def register(body):
    host = body["hostname"]
    if host in registered:                       # the failure that triggers the whole unwind
        log("dns", f"✗ [4] register {host} FAILED — already owned by {registered[host]}")
        return 409, {"reason": f"hostname {host} already registered"}
    registered[host] = body["ip"]
    log("dns", f"▶ [4] register {host} → {body['ip']}   ✅ chain complete")
    return 200, {"hostname": host, "ip": body["ip"]}


def state(_):
    return 200, {"registered": registered}


serve("dns", 8104, {"/register": register, "/_state": state})
