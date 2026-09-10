"""Cache key construction — domain knowledge, deliberately not in the adapter.

The key layout encodes which writes invalidate which reads. That is a statement
about the domain, not about Redis, so it lives where the use cases can see it
and be tested against it. The Redis adapter only ever receives strings.

Layout: service : key-schema-version : entity : id : projection
See scenarios/04_key_strategy for what each segment prevents.
"""

SERVICE = "identity"
SCHEMA = "v1"

DEFAULT_TTL = 300
# Shorter than the profile TTL on purpose: for an authorization answer the TTL
# is the revocation latency, so it is a security parameter. See scenario 05.
AUTHZ_TTL = 30


def user_profile(user_id: int) -> str:
    return f"{SERVICE}:{SCHEMA}:user:{user_id}:profile"


def user_profile_at(user_id: int, version: int) -> str:
    return f"{user_profile(user_id)}@{version}"


def org_generation(org_id: int) -> str:
    """One INCR invalidates every cached list for the org. See scenario 07."""
    return f"catalog:{SCHEMA}:org:{org_id}:gen"


def visible_datasets(org_id: int, generation: int, subject_id: int) -> str:
    """The subject is IN THE KEY. Scenario 05 is what happens when it is not."""
    return f"catalog:{SCHEMA}:g{generation}:org:{org_id}:subject:{subject_id}:datasets"


def visible_datasets_LEAKY(org_id: int, generation: int, subject_id: int) -> str:
    """Deliberately wrong, reachable via ?leaky_key=true, so you can watch one
    user get served another user's rows. Do not copy this into anything."""
    return f"catalog:{SCHEMA}:g{generation}:org:{org_id}:datasets"
