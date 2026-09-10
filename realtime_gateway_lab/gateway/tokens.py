# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Mint HS256 tokens for the seeded users. `./lab.sh token alice`

Hand-rolled rather than PyJWT so the verifier in gateway/authz/main.py and this
minter are visibly the same three steps. Use a library in real code — the point
here is that you can read what a JWT actually is: two base64url JSON documents
and an HMAC over the text of both, joined by dots.

What is IN the token matters as much as the signature. `role` is here because
the gateway needs it for coarse RBAC and it changes rarely. The user's dataset
grants are NOT here, and must not be: they change often, they are large, and a
token is a cache with an expiry you cannot shorten after issuing it.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

SECRET = os.environ.get("LAB_JWT_SECRET", "dev-jwt-secret")

USERS = {
    "alice": {"sub": 1, "role": "admin", "org": 100},
    "bob": {"sub": 2, "role": "member", "org": 100},
    "carol": {"sub": 3, "role": "admin", "org": 200},
    "dan": {"sub": 4, "role": "guest", "org": 100},
}


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint(who: str, ttl: int = 3600, jti: str | None = None) -> str:
    if who not in USERS:
        raise SystemExit(f"unknown user {who!r}; try: {', '.join(USERS)}")
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = dict(USERS[who])
    claims |= {
        # jti is what makes a token revocable. Without it the only way to
        # invalidate one issued token is to rotate the signing key and
        # invalidate every token — see scenario 04.
        "jti": jti or uuid.uuid4().hex[:12],
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl,
    }
    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = b64(hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


if __name__ == "__main__":
    who = sys.argv[1] if len(sys.argv) > 1 else "alice"
    ttl = int(sys.argv[2]) if len(sys.argv) > 2 else 3600
    jti = sys.argv[3] if len(sys.argv) > 3 else None
    print(mint(who, ttl, jti))
