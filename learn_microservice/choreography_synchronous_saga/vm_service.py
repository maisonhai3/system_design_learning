import itertools
from svc import serve, call, log, Downstream

DISK = "http://127.0.0.1:8102/attach"
ids = itertools.count(1)
running = {}                                   # env -> vm_id  (this service's private state)


def root(body):
    return body.get("reason", body)


def provision(body):
    env = body["env"]
    vm = f"vm-{next(ids):03d}"
    running[env] = vm
    log("vm", f"▶ [1] allocate {vm}          running={list(running.values())}")
    try:
        call("vm", DISK, env=env, vm_id=vm, hostname=body["hostname"])
    except Downstream as d:                     # a later step failed -> undo MY step
        del running[env]
        log("vm", f"◀ [1] compensate: deallocate {vm}   running={list(running.values())}")
        return 409, {"env": env, "status": "ROLLED_BACK", "reason": root(d.body)}
    return 201, {"env": env, "status": "PROVISIONED", "vm": vm}


def state(_):
    return 200, {"running_vms": running}


serve("vm", 8101, {"/provision": provision, "/_state": state})
