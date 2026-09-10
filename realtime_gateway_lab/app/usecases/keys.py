"""Stream and key names. Domain knowledge, so it lives with the use cases.

Same four-segment convention as this repo's redis_cache_lab
(service:schema:entity:id:projection), for the same reasons: one team owns a
prefix, the schema version survives a rolling deploy, and the projection keeps
things invalidated by different writes in different keys.
"""

SERVICE = "feed"
SCHEMA = "v1"

# The naive design: one stream, everybody listening, filtering in Python.
# Scenario 02 measures what that costs.
GLOBAL_STREAM = f"{SERVICE}:{SCHEMA}:stream:global"

# How many entries to keep per stream. `~` (approximate) trimming lets Redis
# stop at a node boundary instead of walking to an exact count.
#
# This number is not a tuning detail, it is the resume window: a client offline
# for longer than MAXLEN events has had its cursor trimmed away, and the server
# must be able to say so rather than silently starting them at "now". Scenario
# 01 proves that case.
MAXLEN = 1000


def user_stream(user_id: int) -> str:
    """Fan-out on write: one mailbox per subject."""
    return f"{SERVICE}:{SCHEMA}:stream:user:{user_id}"


def body(event_id: str) -> str:
    """The event body, stored once, for the pointer fan-out in scenario 02."""
    return f"{SERVICE}:{SCHEMA}:body:{event_id}"


def authz_decision(token_id: str, method: str, path: str) -> str:
    """The gateway's cached RBAC verdict. Keyed by the TOKEN, not the user:
    two tokens for the same user can carry different scopes."""
    return f"authz:{SCHEMA}:tok:{token_id}:{method}:{path}"


def revoked_token(token_id: str) -> str:
    """A denylist entry. See scenario 04 for why a denylist and a decision
    cache have to agree about time, and what happens when they do not."""
    return f"authz:{SCHEMA}:revoked:{token_id}"
