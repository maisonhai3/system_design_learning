# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "psycopg[binary]", "redis"]
# ///
"""Scenario 03 — the trust boundary: who is allowed to say who you are."""

import hashlib
import hmac
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import DIRECT, GATEWAY, SIGNED, Lab, compose, reset  # noqa: E402

GATEWAY_SECRET = "dev-gateway-secret"
PAYROLL = 11

lab = Lab(
    "03 — THE TRUST BOUNDARY",
    "The gateway proves who you are. Then it tells the upstream in a header.",
)
reset()


def gateway_assertion(subject: int, role: str, org: int) -> dict:
    """Exactly what the gateway would compute for this identity."""
    message = f"{subject}|{role}|{org}".encode()
    return {
        "X-Auth-Subject": str(subject),
        "X-Auth-Role": role,
        "X-Auth-Org": str(org),
        "X-Auth-Signature": hmac.new(
            GATEWAY_SECRET.encode(), message, hashlib.sha256
        ).hexdigest(),
    }


# ---------------------------------------------------------------------------
lab.section("How the gateway tells the upstream who you are")
lab.note(
    """
    The mechanism is the whole story: the gateway verifies a signature, and
    then hands the result to the upstream as PLAIN HEADERS. The upstream does
    not re-verify the token — that is the point of a gateway, and it is also
    the vulnerability, because a header is a thing anyone can type.
    """
)

alice = lab.actor("alice", "alice")
saw = alice.json(
    "GET",
    "/debug/headers",
    headers={"X-Auth-Subject": "9999", "X-Auth-Role": "superuser", "X-User-Id": "3"},
)
headers = saw["x_headers"]

lab.held(
    headers.get("x-auth-subject") == "1"
    and headers.get("x-auth-role") == "admin"
    and "x-user-id" not in headers,
    f"the forged headers never reached the service; it saw subject={headers.get('x-auth-subject')}",
    """
    Two Traefik middlewares did that, and BOTH are load-bearing:

      strip-client-auth   deletes every header in the trusted namespace from
                          the incoming request — including names the auth
                          service does not set, like X-User-Id.
      forwardAuth         copies its own values in via authResponseHeaders.

    Order matters and is not obvious: strip runs first, so forward-auth writes
    onto a request that no longer carries anything forged. Reverse them and a
    header the auth service does not happen to set survives exactly as the
    client sent it. Belt and braces, because the list in authResponseHeaders is
    edited by people who are adding a feature, not auditing a boundary.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The anomaly: the gateway is a route, not a wall")
lab.note(
    f"""
    The upstream is listening on {DIRECT}. In this lab that port is published
    on purpose; in a cluster you would not publish it — and it would still be
    reachable from every pod on the network, every sidecar, every debug shell,
    and anything that gets to run code inside your perimeter.

    Nobody needs to break the gateway. They just need to not use it.
    """
)

anonymous = lab.actor("attacker", None, base=DIRECT)
forged = anonymous.json("GET", "/whoami", headers={
    "X-Auth-Subject": "1", "X-Auth-Role": "admin", "X-Auth-Org": "100",
})

lab.broke(
    forged.get("subject") == 1 and forged.get("role") == "admin",
    f"three headers and no token: the service believes it is talking to {forged.get('subject')} ({forged.get('role')})",
    """
    That is not a privilege escalation, it is an identity vending machine. The
    attacker did not steal a token, guess a password, or exploit a bug. They
    typed a header that the service was designed to trust.
    """,
)

# And a forged identity is a real identity as far as everything downstream goes.
published = anonymous.json(
    "POST", f"/events/{PAYROLL}?mode=fanout",
    headers={"X-Auth-Subject": "1", "X-Auth-Role": "admin", "X-Auth-Org": "100"},
)
lab.broke(
    "event_id" in published,
    "the forged admin published an event into the restricted feed",
    """
    Read that once more: writes, not just reads. Every audit row this produced
    names Alice. The incident review will start from her account.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The same thing from inside the network, where you cannot see it")
lab.note(
    """
    Doing this from the host needed a published port. Doing it from any
    container on the same network needs nothing at all — no published port, no
    ingress rule, no exception in a security group.

    Running it from the authorization service's own container, which is about
    as trusted as a component gets.
    """
)

inside = compose(
    "exec", "-T", "authz", "python", "-c",
    "import urllib.request, json;"
    "r = urllib.request.Request('http://api:8000/whoami', headers={"
    "'X-Auth-Subject': '3', 'X-Auth-Role': 'admin', 'X-Auth-Org': '200'});"
    "print(urllib.request.urlopen(r).read().decode())",
)
inside_result = json.loads(inside.stdout.strip() or "{}") if inside.returncode == 0 else {}
print(f"     authz container → api:8000 → {inside.stdout.strip() or inside.stderr.strip()[:120]}")

lab.broke(
    inside_result.get("subject") == 3,
    "a neighbouring container asserted a different org's admin and was believed",
    """
    This is the version that matters, because it does not depend on a
    misconfigured port. Inside a cluster, "only the gateway can reach this
    service" is a NETWORK claim — a NetworkPolicy, a mesh authorization
    policy, a security group — and it is maintained by different people, in a
    different repository, from the code that depends on it.

    A code-level guarantee that rests on an unwritten network assumption is not
    a guarantee. It is a coincidence with good documentation.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The fix: make the assertion unforgeable, not merely unrouted")
lab.note(
    """
    The gateway signs what it asserts, and the upstream verifies. Same code,
    same image, one environment variable: LAB_SUBJECT_TRUST=signed.

    The signature covers the exact fields being asserted, so a client cannot
    keep a valid signature and swap the subject it was issued for.
    """
)

signed_service = lab.actor("attacker", None, base=SIGNED)
rejected = signed_service.json("GET", "/whoami", headers={
    "X-Auth-Subject": "1", "X-Auth-Role": "admin", "X-Auth-Org": "100",
})

lab.held(
    "detail" in rejected and "signed" in rejected["detail"],
    f"the same forgery is rejected: {rejected.get('detail')}",
    """
    And note what happens with NO headers at all: the service returns 401
    rather than falling back to anonymous. Fail closed. A request that skipped
    authentication has not passed it.
    """,
)

no_headers = signed_service.json("GET", "/whoami")
lab.held(
    "detail" in no_headers,
    "with no assertion at all, the service refuses rather than guessing",
    "",
)

# A legitimate, correctly-signed assertion still works — the check is a check,
# not a wall.
legit = signed_service.json("GET", "/whoami", headers=gateway_assertion(2, "member", 100))
lab.held(
    legit.get("subject") == 2 and legit.get("provenance") == "gateway (signed)",
    "a correctly signed assertion is accepted, so the boundary is a check and not a block",
    "",
)


# ---------------------------------------------------------------------------
lab.section("What the signature does NOT fix")
lab.note(
    """
    Say the honest limit before an interviewer finds it. The assertion signs
    (subject, role, org) and nothing else — no timestamp, no nonce, no
    request binding. So it is a bearer credential with no expiry.
    """
)

replayed = signed_service.json("GET", "/whoami", headers=gateway_assertion(1, "admin", 100))
lab.broke(
    replayed.get("subject") == 1,
    "an assertion captured once is valid forever, for any request, from anywhere",
    """
    Anyone who observes one gateway→upstream request — a mesh sidecar, a
    tcpdump, an over-eager logging proxy, a heap dump — holds a permanent
    admin credential. The signature proved the assertion was not EDITED. It
    never proved it was FRESH, or that it belonged to this request.

    What actually closes it, in increasing order of cost:

      add exp + nonce      make the assertion short-lived and single-use. Cheap,
                           and now the upstream needs a clock and a nonce store.
      bind to the request  sign method + path + a body hash, so an assertion
                           for GET /whoami cannot be replayed onto DELETE.
      mTLS between hops    the upstream accepts connections only from a peer
                           holding the gateway's client certificate. The header
                           stops being a credential at all, which is the real
                           fix — a service mesh gives you this without code.
      keep the token       forward the original JWT and re-verify it at the
                           upstream. Costs a verification per hop and a shared
                           key distribution problem, and buys you an
                           end-to-end identity nothing in the middle can mint.

    Which one you pick is a threat-model decision. Not having noticed the
    question is the wrong answer.
    """,
)

lab.takeaway(
    """
    "A gateway authenticates and then tells the upstream who you are in a
     header, so two things have to be true. First, the gateway must strip
     every header in that trusted namespace on the way in — not only overwrite
     the ones it sets, because the list of headers it sets is edited by people
     adding features. Second, the upstream must fail closed when the header is
     absent, so a request that skipped the gateway is refused rather than
     treated as anonymous.

     But 'only the gateway can reach this service' is a network claim
     maintained in a different repo by different people. So I sign the
     assertion — the gateway HMACs (subject, role, org) and the upstream
     verifies. The honest limit is that a signature without an expiry or a
     request binding is a permanent bearer credential to anyone who sees one,
     so in a real system that becomes mTLS between hops, or a short-lived
     assertion bound to the request."
    """
)

lab.finish()
