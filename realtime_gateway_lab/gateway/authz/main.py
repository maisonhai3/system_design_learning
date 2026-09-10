"""The shared authorization service — the gateway's ForwardAuth target.

This is the job description's "lớp authorization dùng chung cho toàn hệ thống":
one service that every route in every upstream is checked against, so the token
format, the clock skew allowance, the denylist and the RBAC table exist once.

What it CAN decide, from a token and a request line:
  * is this token real, unexpired, and not revoked        (authentication)
  * does the role it carries reach this method+path at all (coarse RBAC)

What it CANNOT decide, and must not pretend to:
  * may this subject see THIS ROW                          (ABAC)
It has never seen the datasets table, and giving it a database connection so it
could would make it an upstream service with a gateway's blast radius. The
service that owns the data owns the row-level decision. That division is the
whole of scenario 04.

Contract with Traefik's ForwardAuth:
  in   the original method and path arrive as X-Forwarded-Method / -Uri
  out  2xx  → allow; listed response headers are copied onto the upstream request
       401/403 → the response is returned to the client verbatim
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import redis.asyncio as aioredis
from fastapi import FastAPI, Header, Request, Response

JWT_SECRET = os.environ.get("LAB_JWT_SECRET", "dev-jwt-secret")
GATEWAY_SECRET = os.environ.get("LAB_GATEWAY_SECRET", "dev-gateway-secret")
REDIS_URL = os.environ.get("LAB_REDIS_URL", "redis://redis:6379/0")

# How long a positive RBAC verdict may be reused. This number IS the revocation
# latency for anything the denylist cannot reach — see scenario 04, and
# redis_cache_lab's scenario 05 for the same lesson on the data side.
DECISION_TTL = int(os.environ.get("LAB_DECISION_TTL", "30"))

# Coarse RBAC. Route patterns, not rows. Deliberately tiny: everything that
# needs to be bigger than this belongs in the service, not in the gateway.
POLICY: list[tuple[str, str, set[str]]] = [
    ("POST", "/events", {"admin"}),
    ("POST", "/grants", {"admin"}),
    ("DELETE", "/grants", {"admin"}),
    ("GET", "/datasets", {"admin", "member", "guest"}),
    ("GET", "/feed", {"admin", "member", "guest"}),
    ("GET", "/whoami", {"admin", "member", "guest"}),
    ("GET", "/debug", {"admin"}),
]

app = FastAPI(title="Shared Authorization Service")
redis_client: aioredis.Redis | None = None


@app.on_event("startup")
async def _startup():
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def verify_jwt(token: str) -> dict | None:
    """HS256 verification, written out rather than imported.

    Not because you should hand-roll this — use PyJWT — but because the three
    things that actually go wrong are invisible when a library does them:

      1. verifying the signature over `header.payload` EXACTLY as received,
         not over a re-serialised payload (canonicalisation bugs live here);
      2. `compare_digest`, so the comparison does not leak the MAC by timing;
      3. checking `exp` at all. A library will; a hand-rolled parser written in
         a hurry frequently will not, and "expired tokens still work" is not a
         finding anyone makes by testing the happy path.
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return None

    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
        return None

    claims = json.loads(_b64url_decode(payload_b64))
    if claims.get("exp", 0) < time.time():
        return None
    return claims


def rbac_allows(role: str, method: str, path: str) -> tuple[bool, str]:
    for policy_method, prefix, roles in POLICY:
        if method == policy_method and path.startswith(prefix):
            return (role in roles), f"{policy_method} {prefix}* requires {sorted(roles)}"
    # Default deny. An unlisted route is not an oversight to be waved through;
    # it is a route nobody has written a policy for, and shipping it open is
    # how a debug endpoint ends up on the internet.
    return False, "no policy matches this route (default deny)"


def sign_subject(subject_id: str, role: str, org: str) -> str:
    message = f"{subject_id}|{role}|{org}".encode()
    return hmac.new(GATEWAY_SECRET.encode(), message, hashlib.sha256).hexdigest()


@app.get("/health")
async def health():
    return {"ok": True, "decision_ttl": DECISION_TTL}


@app.api_route("/auth", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def auth(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    x_forwarded_method: str | None = Header(default=None),
    x_forwarded_uri: str | None = Header(default=None),
):
    method = x_forwarded_method or request.method
    path = (x_forwarded_uri or "/").split("?")[0]

    if not authorization or not authorization.lower().startswith("bearer "):
        return Response(
            content=json.dumps({"error": "missing bearer token"}),
            status_code=401,
            media_type="application/json",
        )

    token = authorization.split(" ", 1)[1]
    claims = verify_jwt(token)
    if claims is None:
        return Response(
            content=json.dumps({"error": "invalid or expired token"}),
            status_code=401,
            media_type="application/json",
        )

    jti = claims.get("jti", "")
    subject, role, org = str(claims.get("sub")), claims.get("role", "guest"), str(claims.get("org"))

    # The denylist is checked BEFORE the decision cache, every time, with no
    # TTL of its own. If it were behind the cache, revoking a token would take
    # up to DECISION_TTL seconds to bite — see scenario 04, where that is
    # exactly what happens when the ordering is reversed.
    if await redis_client.exists(f"authz:v1:revoked:{jti}"):
        return Response(
            content=json.dumps({"error": "token revoked"}),
            status_code=401,
            media_type="application/json",
        )

    decision_key = f"authz:v1:tok:{jti}:{method}:{path}"
    cached = await redis_client.get(decision_key)
    if cached == "allow":
        allowed, reason, source = True, "cached", "cache"
    elif cached == "deny":
        allowed, reason, source = False, "cached", "cache"
    else:
        allowed, reason = rbac_allows(role, method, path)
        # Deliberately asymmetric TTLs. A stale ALLOW is a security incident; a
        # stale DENY is a support ticket. They should not share a number.
        await redis_client.set(
            decision_key,
            "allow" if allowed else "deny",
            ex=DECISION_TTL if allowed else DECISION_TTL * 4,
        )
        source = "computed"

    if not allowed:
        return Response(
            content=json.dumps({"error": "forbidden", "reason": reason}),
            status_code=403,
            media_type="application/json",
        )

    # These are copied onto the upstream request by Traefik's
    # authResponseHeaders. They OVERWRITE whatever the client sent under the
    # same names, which is the mechanism that closes the header-injection hole
    # in scenario 03 — but only for the headers actually listed there.
    response.headers["X-Auth-Subject"] = subject
    response.headers["X-Auth-Role"] = role
    response.headers["X-Auth-Org"] = org
    response.headers["X-Auth-Signature"] = sign_subject(subject, role, org)
    response.headers["X-Auth-Source"] = source
    return {"allowed": True, "subject": subject, "role": role, "source": source}
