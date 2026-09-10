import itertools
from svc import serve, call, log, Downstream

IP = "http://127.0.0.1:8103/assign"
ids = itertools.count(1)
attached = {}


def root(body):
    return body.get("reason", body)


def attach(body):
    env = body["env"]
    disk = f"disk-{next(ids):03d}"
    attached[env] = disk
    log("disk", f"▶ [2] attach {disk} → {body['vm_id']}")
    try:
        call("disk", IP, env=env, hostname=body["hostname"])
    except Downstream as d:
        del attached[env]
        log("disk", f"◀ [2] compensate: detach {disk}")
        return 409, {"env": env, "reason": root(d.body)}
    return 200, {"env": env, "disk": disk}


def state(_):
    return 200, {"attached_disks": attached}


serve("disk", 8102, {"/attach": attach, "/_state": state})
