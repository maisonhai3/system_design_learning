FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
# A stub package directory lets `pip install .` (setuptools needs `app/` to
# exist for `packages = ["app"]`) run here, before the real source is
# copied in, so this layer only gets invalidated by pyproject.toml changes,
# not by every source edit. The real `app/` copied below overwrites it.
RUN mkdir -p app && touch app/__init__.py && pip install --no-cache-dir ".[dev]"

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
