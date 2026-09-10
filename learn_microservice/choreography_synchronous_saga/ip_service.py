from svc import serve, call, log, Downstream

DNS = "http://127.0.0.1:8104/register"
free = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
assigned = {}


def root(body):
    return body.get("reason", body)


def assign(body):
    env = body["env"]
    ip = free.pop(0)
    assigned[env] = ip
    log("ip", f"▶ [3] assign IP {ip}        free={free}")
    try:
        call("ip", DNS, env=env, hostname=body["hostname"], ip=ip)
    except Downstream as d:
        free.insert(0, assigned.pop(env))       # return it to the pool
        log("ip", f"◀ [3] compensate: release IP {ip}   free={free}")
        return 409, {"env": env, "reason": root(d.body)}
    return 200, {"env": env, "ip": ip}


def state(_):
    return 200, {"assigned": assigned, "free": free}


serve("ip", 8103, {"/assign": assign, "/_state": state})
