# The trust boundary — who is allowed to say who you are

**The claim you are practising:** *"A gateway authenticates and then tells the
upstream who you are in a header. So the gateway must strip that namespace on
the way in, the upstream must fail closed without it — and 'only the gateway can
reach this service' is a network claim, not a code one."*

## The mechanism, which is also the vulnerability

```
client ──JWT──▶ Traefik ──▶ authz service (verify signature, coarse RBAC)
                   │
                   └──▶ api    X-Auth-Subject: 1
                               X-Auth-Role: admin
                               X-Auth-Org: 100
```

The upstream does not re-verify the token. That is the *point* of a gateway —
one place that knows the token format, the clock-skew allowance and the
denylist. It is also the hole, because **a header is a thing anyone can type.**

## What closes it at the front door

Send a forged header through the gateway and it never arrives:

```bash
./lab.sh as alice -H "X-Auth-Subject: 9999" -H "X-User-Id: 3" \
    http://localhost:8090/debug/headers
# the service saw: x-auth-subject: 1, x-auth-role: admin, and no x-user-id
```

Two Traefik middlewares did that, and **both are load-bearing**:

| middleware | what it does |
|---|---|
| `strip-client-auth` | deletes *every* header in the trusted namespace from the incoming request — including names the auth service does not set, like `X-User-Id` |
| `forwardAuth` | copies its own values in via `authResponseHeaders` |

Order matters and is not obvious: **strip runs first**, so forward-auth writes
onto a request that no longer carries anything forged. Reverse them and a header
the auth service does not happen to set survives exactly as the client sent it.

Why both, when `authResponseHeaders` overwrites the names it lists? Because that
list is edited by people adding a feature, not by people auditing a boundary.
`X-Auth-Tenant` gets added to the service and not to the list, and now it is
client-controlled. Belt and braces.

The same pattern in nginx (`gateway/nginx/nginx.conf`) is `auth_request` +
`auth_request_set` + `proxy_set_header X-User-Id "";` — different syntax,
identical shape, identical failure modes.

## The anomaly: the gateway is a route, not a wall

```bash
curl -H "X-Auth-Subject: 1" -H "X-Auth-Role: admin" -H "X-Auth-Org: 100" \
     http://localhost:8093/whoami
# {"subject": 1, "role": "admin", "provenance": "gateway"}
```

Three headers and no token. That is not a privilege escalation, it is an
**identity vending machine** — and it works for writes too: the lab publishes an
event into the restricted feed as forged-Alice, and every audit row names her.

### The version that does not need a published port

```
authz container ──▶ http://api:8000/whoami   with forged headers
{"subject": 3, "role": "admin", "org_id": 200}
```

Run from the *authorization service's own container* — about as trusted as a
component gets. Inside a cluster, "only the gateway can reach this service" is a
**NetworkPolicy**, a mesh authorization policy, a security group: maintained by
different people, in a different repository, from the code that depends on it.

> A code-level guarantee that rests on an unwritten network assumption is not a
> guarantee. It is a coincidence with good documentation.

## The fix: make the assertion unforgeable, not merely unrouted

The gateway HMACs what it asserts; the upstream verifies. Same image, same code,
one environment variable (`LAB_SUBJECT_TRUST=signed`):

```
forged headers          → 401 "subject headers are not signed by the gateway"
no headers at all       → 401 (fail closed — a check that could not be made
                               has not been passed)
correctly signed        → 200
```

The signature covers the exact fields being asserted, so a client cannot keep a
valid signature and swap the subject it was issued for. Use `compare_digest`, not
`==`: a naive comparison leaks the MAC's prefix through timing, and it is free to
get right.

## What the signature does NOT fix

Say this before an interviewer says it for you. The assertion signs
`(subject, role, org)` and nothing else — **no timestamp, no nonce, no request
binding**. It is a bearer credential with no expiry:

```
an assertion captured once is valid forever, for any request, from anywhere
```

Anyone who observes one gateway→upstream request — a mesh sidecar, a `tcpdump`,
an over-eager logging proxy, a heap dump — holds a permanent admin credential.
The signature proved the assertion was not *edited*. It never proved it was
*fresh*.

What actually closes it, in increasing order of cost:

| | |
|---|---|
| add `exp` + nonce | short-lived and single-use. Cheap; now the upstream needs a clock and a nonce store. |
| bind to the request | sign method + path + a body hash, so an assertion for `GET /whoami` cannot be replayed onto a `DELETE`. |
| **mTLS between hops** | the upstream accepts connections only from a peer holding the gateway's certificate. The header stops being a credential at all. A service mesh gives you this without code. |
| forward the original JWT | re-verify at the upstream. Costs a verification per hop and key distribution, buys an end-to-end identity nothing in the middle can mint. |

Which one you pick is a threat-model decision. Not having noticed the question is
the wrong answer.

## Run it

```bash
./lab.sh run 03

./lab.sh as bob /debug/headers          # what the gateway did to your request
./lab.sh direct /whoami                 # bypass it entirely
curl -H "X-Auth-Subject: 1" -H "X-Auth-Role: admin" -H "X-Auth-Org: 100" \
     http://localhost:8096/whoami       # the signed service says no
```

## The interview answer

> *"Your gateway authenticates and passes the user id to the service in a
> header. Any problem with that?"*

"Two, and they're different kinds. First, the gateway has to *strip* that whole
header namespace on the way in, not just overwrite the names it sets — because
the list of names it sets is maintained by people adding features. And the
upstream has to fail closed when the header is absent, so a request that skipped
the gateway is refused rather than treated as anonymous.

Second, and this is the one that actually bites: 'only the gateway can reach this
service' is a network claim, kept in a different repo by different people. Any
pod on the network, any sidecar, any debug shell can send those headers. So I
sign the assertion — HMAC over subject, role and org, verified upstream. The
honest limit is that a signature with no expiry or request binding is a permanent
bearer credential to anyone who sees one, so in a real system that becomes mTLS
between hops or a short-lived assertion bound to the request."
