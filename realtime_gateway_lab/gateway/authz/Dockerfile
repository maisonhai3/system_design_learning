FROM python:3.12-slim
WORKDIR /srv
# See app/Dockerfile for why this is a secret mount rather than --trusted-host.
RUN --mount=type=secret,id=ca,target=/run/secrets/ca.crt \
    if [ -s /run/secrets/ca.crt ]; then export PIP_CERT=/run/secrets/ca.crt; fi; \
    pip install --no-cache-dir \
        "fastapi==0.115.*" "uvicorn[standard]==0.32.*" "redis==5.2.*"
COPY gateway/authz/main.py /srv/main.py
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
