import sys, time
from svc import call, Downstream

VM = "http://127.0.0.1:8101/provision"
ENVS = {
    "alpha": dict(env="alpha", hostname="alpha.corp"),
    "beta":  dict(env="beta",  hostname="alpha.corp"),   # config bug: reuses alpha's hostname
}

for name in sys.argv[1:]:
    if name == "STATE":
        print("\n────────── final state of every service ──────────")
        for svc, url in [("vm","8101"),("disk","8102"),("ip","8103"),("dns","8104")]:
            print(f"   {svc:>5}: {call('client', f'http://127.0.0.1:{url}/_state')}", flush=True)
        continue
    print(f"\n════════════ provision {name} ════════════", flush=True)
    t0 = time.perf_counter()
    try:
        res = call("client", VM, **ENVS[name])
    except Downstream as d:
        res = d.body
    print(f"   client verdict in {1000*(time.perf_counter()-t0):.0f} ms: {res}", flush=True)
