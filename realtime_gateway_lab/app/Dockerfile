FROM python:3.12-slim

WORKDIR /srv

# Networks that re-sign TLS — corporate proxies, some CI runners — break
# `pip install` with "certificate verify failed". The fix is to TRUST the extra
# CA, never to pass --trusted-host, which turns verification off entirely and
# is the reflex answer you will see in every stale Stack Overflow thread:
#
#     docker build --secret id=ca,src=/path/to/ca-bundle.crt .
#
# With no secret supplied the mount target simply does not exist, `[ -s ]` is
# false, and this is a plain pip install. So the same Dockerfile works on a
# normal laptop and behind an inspecting proxy, with no branch to maintain.
RUN --mount=type=secret,id=ca,target=/run/secrets/ca.crt \
    if [ -s /run/secrets/ca.crt ]; then export PIP_CERT=/run/secrets/ca.crt; fi; \
    pip install --no-cache-dir \
        "fastapi==0.115.*" \
        "uvicorn[standard]==0.32.*" \
        "sqlalchemy[asyncio]==2.0.*" \
        "asyncpg==0.30.*" \
        "redis==5.2.*"

COPY app /srv/app

# One worker on purpose. Scenario 06 is about what a long-lived connection
# costs, and that is only legible when you can count the workers it is costing.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
