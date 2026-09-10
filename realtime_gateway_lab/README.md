# Realtime Gateway Lab — a push feed behind a real API gateway

A realtime feed on SSE, an authorization layer shared across services, and a
gateway you can actually break — Traefik and nginx in Docker, no cloud.

It reuses `redis_cache_lab`'s domain on purpose: the same Alice and Bob, the
same datasets, the same one grant row. That lab asks *"what does it cost to
**cache** an authorization answer?"* This one asks *"what does it cost to
**push** one, and who is allowed to decide it?"* — so you can compare the two
answers instead of learning two toy worlds.

Every scenario is an **automated proof**: it forces the situation, asserts the
anomaly actually reproduced, asserts the fix actually held, and exits non-zero if
either stops being true.

## Quick start

```bash
./lab.sh up                  # redis, postgres, api, authz, traefik, nginx
./lab.sh as alice /whoami    # through the gateway, with a real JWT
./lab.sh list
./lab.sh run 03              # the one the gateway exists for
./lab.sh run-all             # every proof; non-zero if any claim stops holding
```

Watch a feed in one terminal and push into it from another:

```bash
./lab.sh sse alice           # opens an SSE stream, prints frames as they land
./lab.sh publish 11          # restricted payroll — only Alice's stream gets it
./lab.sh sse bob             # ...and Bob's stream never contained it
```

## The scenarios

| # | Scenario | The claim you'll be able to defend |
|---|---|---|
| 01 | The resume cursor | Pub/Sub has no yesterday. Streams do, and the stream id *is* the SSE cursor. The `0-0` default replays your whole history to every new tab. |
| 02 | **Fan-out on write** | Measured: 50 listeners → **50 authorization checks to make 1 delivery**. And fan-out *freezes* the decision — the cost nobody mentions. |
| 03 | **The trust boundary** | Three headers and no token is an identity vending machine. "Only the gateway can reach this service" is a network claim, not a code one. |
| 04 | RBAC at the gateway, ABAC at the service | The gateway has never seen your data. Putting the role in the token makes revocation latency = the token's remaining lifetime. |
| 05 | SSE through the gateway | Measured: the same endpoint delivers in **0.02s to curl and never to a gzip-capable client**. |
| 06 | The connection budget | Measured: **12 idle browser tabs → an unrelated endpoint goes 9ms → 6000ms**. The fix is a type signature. |

Every number above was measured by the runners on the machine that built this.
`./lab.sh run-all` re-measures them on yours.

## What's running

```
        :8090  Traefik ──ForwardAuth──▶ authz  :8094   JWT, coarse RBAC, decisions cached
           │                              ▲
           │                              │
           └──────────────────────────▶  api   :8093   ABAC, feeds, published ON PURPOSE
        :8092  nginx ──auth_request──────┘             two locations: /buffered/ /streamed/
        :8091  Traefik dashboard
                              api-signed  :8096        same image, requires a signed assertion
```

Six containers, one command. The two gateways run the **same pattern in two
syntaxes** — Traefik's `forwardAuth` and nginx's `auth_request` — because knowing
they are the same idea, with the same failure modes, is worth more than knowing
either one.

| port | what | why it's there |
|---|---|---|
| 8090 | Traefik, the front door | routes, middlewares, ForwardAuth |
| 8091 | Traefik dashboard | see the routers and middlewares resolve |
| 8092 | nginx | `/buffered/` and `/streamed/` differ only in buffering — scenario 05 |
| 8093 | the API, directly | **published on purpose**: scenario 03 is about what that costs |
| 8094 | the authz service | call `/auth` by hand and see what the gateway sees |
| 8096 | the API in `signed` mode | same code, one env var — scenario 03's fix |

## Play with it

```bash
./lab.sh token alice                    # mint a JWT (alice, bob, carol, dan)
./lab.sh as bob /datasets               # ABAC: Bob's rows, not Alice's
./lab.sh as dan /datasets               # a guest sees public only
./lab.sh as bob -X POST http://localhost:8090/events/11   # 403 at the gateway

./lab.sh as alice /debug/headers        # what the gateway did to your request
./lab.sh as alice -H "X-Auth-Subject: 3" /debug/headers   # ...and to your forgery

./lab.sh direct /whoami                 # bypass the gateway entirely
./lab.sh revoke-token <jti>             # denylist one token
./lab.sh streams                        # whose mailbox holds what
./lab.sh status                         # streams, decision cache, TTLs
```

## Layout

```
lab.sh                     one entry point for everything
docker-compose.yml         six services; ports chosen to miss the other labs
schema/                    01_schema.sql, 02_seed.sql — same domain as redis_cache_lab
gateway/
    traefik/traefik.yml    static config: entrypoints, providers, timeouts
    traefik/dynamic.yml    routers, middlewares, services — hot-reloaded
    nginx/nginx.conf       auth_request, and buffering on vs off
    authz/main.py          the shared authorization service (JWT + coarse RBAC)
    tokens.py              mint test tokens; hand-rolled so you can read a JWT
app/
    domain/                entities + Protocols. No framework imports at all.
    usecases/              authorize (one policy), publish (fan-out), subscribe (SSE)
    adapters/              SQLAlchemy async, redis.asyncio streams
    api/                   routers + the trust-boundary dependency
scenarios/NN_name/
    README.md              what breaks, why, the fixes, and the interview answer
    run.py                 the automated proof
lab/harness.py             HTTP actors, SSE sessions that can be cut off and resumed
```

The architecture's grep test — the inner layers must pass it:

```bash
grep -rn "fastapi\|sqlalchemy\|redis" app/domain app/usecases --include='*.py'
```

Docstring mentions only, no imports. `app/main.py` and `app/api/` are allowed to
know about FastAPI; nothing below them is.

## On proving this kind of thing

Two failures here cannot be asserted about a single request, and the harness is
shaped around that:

- **Resume** needs a connection that is cut off *without a goodbye* and a second
  one that carries `Last-Event-ID`. `SseSession.disconnect()` closes the client
  socket — not just a flag, because a flag only stops *this* side reading while
  the server keeps everything the request holds alive. Getting that wrong first
  is how the harness learned scenario 06.
- **The trust boundary** needs a request that did not come from the gateway, so
  scenario 03 runs one from *inside* the Docker network, out of another
  container.

And one thing worth copying: scenario 04 asserts that the SQL policy and the
Python policy agree for every (subject, dataset) pair. That test exists because
they **drifted while this lab was being written** — the Python version excluded
guests from internal rows and the SQL version did not. Nobody decides to have two
policies; somebody adds a clause to the one they have open.

## Where this disagrees with the conversation that prompted it

The conversation that led here landed on: Streams over Pub/Sub, `XREAD BLOCK`,
`Last-Event-ID`, fan-out on write, RBAC at the gateway and ABAC at the service.
All correct. Four things it left out, and each is a follow-up question you should
expect:

**1. `Header(default="0-0")` replays your entire history.** A fresh connection
and a resume are different requests. Fresh is `$`. Scenario 01 measures it.

**2. Fan-out on write freezes the authorization decision.** The conversation
treated it as strictly better than filtering on read. It is better on both the
performance and the confidentiality axis, and it buys those by making the
mailbox a durable record of a permission that may since have changed: granting
access later cannot deliver backwards, and revoking does not un-deliver.
Scenario 02 proves both, and the resolution — fan out *pointers*, authorize the
dereference — recovers the property your instinct was protecting.

**3. "The gateway enforces it" is a routing claim.** Anything on the network can
send the headers the gateway sets. Scenario 03 does exactly that from another
container.

**4. A long-lived connection is a held resource.** `Depends(get_session)` on an
SSE endpoint holds a pooled session until the user closes the tab. Scenario 06
measures twelve tabs starving a pool of ten.

## Notes

**Ports 6381, 5437, 8090–8096.** The other labs in this repo already use 6379,
6380, 5432–5436, 8000 and 8001. This one never touches them.

**Deliberately mis-configured, in named places.** nginx `/buffered/` has
`proxy_read_timeout 5s` and `gzip on` for `text/event-stream`; Postgres runs with
`max_connections=30` and a pool of 10; the Traefik dashboard has no auth. Each is
there so a scenario can reach it. Do not copy them anywhere real.

**`X-User-Id` is not authentication.** In `LAB_SUBJECT_TRUST=client` mode the
service believes a header you type, and says so in every response's `provenance`
field. That mode exists so you can poke at the service directly; the containers
run in `gateway` and `signed` mode.

**Behind a TLS-inspecting proxy?** `LAB_BUILD_CA=/path/to/ca-bundle.crt
./lab.sh up` passes the CA to the image builds as a BuildKit secret. Unset on a
normal network, where it is a no-op. The Dockerfiles never disable verification.

**Requirements:** Docker, `psql`, `redis-cli`, and
[`uv`](https://docs.astral.sh/uv/) for the runners — which fetch their own
dependencies via inline script metadata, so there is no virtualenv to manage.

**Verified.** Every scenario, `./lab.sh run-all` (36/36 claims), and the gateway
end to end with `traefik:v3.3`, `nginx:1.27-alpine`, `redis:7-alpine` and
`postgres:16`. The measured numbers above came from those runs.
