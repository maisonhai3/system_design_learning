# postgres_sandbox

A blank PostgreSQL 16 instance. No schema, no init scripts — create your own tables.

## Run

```bash
docker compose up -d
```

## Connect

| | |
|---|---|
| host | `localhost` |
| port | `5434` |
| user | `postgres` |
| password | `postgres` |
| database | `sandbox` |

```bash
psql postgresql://postgres:postgres@localhost:5434/sandbox
```

Or from inside the container:

```bash
docker compose exec postgres psql -U postgres -d sandbox
```

## Notes

- Host port is **5434** because 5432 is the machine's native cluster and 5433 belongs to the `avatar_uploading` lab.
- Data lives in the named volume `postgres-sandbox_pgdata`, so it survives `docker compose down`.
- Override any setting by copying `.env.example` to `.env`.

## Wipe everything

```bash
docker compose down -v
```
