"""SQLAlchemy 2.0 async adapters. The only module that knows what a table is."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.entities import Dataset, User

# The ABAC policy, in SQL. It MUST agree, clause for clause, with
# `app/usecases/authorize.may_see` — scenario 04 asserts that for every
# (subject, dataset) pair, and it does so because this file and that one
# already drifted apart once while this lab was being written: the Python
# policy excluded guests from internal rows and this query did not.
#
# That is the ordinary way it happens. Nobody decides to have two policies;
# somebody adds a clause to the one they are looking at. The defence is not
# discipline, it is a test that fails.
#
# Two details worth keeping:
#   * the org comes from the USERS TABLE, not from the caller. A forged or
#     stale org claim then selects nothing instead of another tenant's rows.
#   * the filter is in the WHERE clause, not in Python afterwards. Rows you
#     never fetched cannot be logged, serialised, or leaked by a later refactor.
VISIBLE_SQL = """
SELECT d.id, d.org_id, d.name, d.classification
FROM datasets d
JOIN users u ON u.id = :uid
WHERE d.org_id = u.org_id
  AND u.org_id = :org
  AND (
        d.classification = 'public'
     OR (d.classification = 'internal' AND u.role <> 'guest')
     OR EXISTS (SELECT 1 FROM dataset_grants g
                WHERE g.dataset_id = d.id AND g.user_id = u.id)
  )
ORDER BY d.id
"""


def make_engine(dsn: str):
    return create_async_engine(dsn, pool_pre_ping=True, pool_size=10, max_overflow=0)


def make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, user_id: int) -> User | None:
        row = (
            await self.session.execute(
                text(
                    "SELECT id, email, display_name, role, org_id FROM users WHERE id = :id"
                ).bindparams(id=user_id)
            )
        ).mappings().first()
        return User(**row) if row else None

    async def all_ids(self) -> list[int]:
        rows = (await self.session.execute(text("SELECT id FROM users ORDER BY id"))).scalars()
        return list(rows)


class SqlAlchemyDatasetRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, dataset_id: int) -> Dataset | None:
        row = (
            await self.session.execute(
                text(
                    "SELECT id, org_id, name, classification FROM datasets WHERE id = :id"
                ).bindparams(id=dataset_id)
            )
        ).mappings().first()
        return Dataset(**row) if row else None

    async def visible_to(self, user_id: int, org_id: int) -> list[Dataset]:
        """ABAC in the WHERE clause, not in Python afterwards.

        Filtering after fetching means the restricted rows were on the wire, in
        memory, and one `logger.debug(rows)` from your log aggregator. The
        database is the last place you can cheaply not-have the data at all.
        """
        rows = (
            await self.session.execute(
                text(VISIBLE_SQL).bindparams(org=org_id, uid=user_id)
            )
        ).mappings().all()
        return [Dataset(**r) for r in rows]

    async def subjects_allowed_to_see(self, dataset_id: int) -> list[int]:
        """The policy, run backwards. See the note on the port.

        Two clauses, mirroring `authorize.may_see`: same-org members and admins
        see open rows, and anyone with an explicit grant sees a restricted one.
        Keeping this in SQL rather than looping in Python is what makes fan-out
        O(one query) instead of O(users).
        """
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT u.id
                    FROM users u
                    JOIN datasets d ON d.id = :did
                    WHERE u.org_id = d.org_id
                      AND (
                            (d.classification = 'public')
                         OR (d.classification = 'internal' AND u.role <> 'guest')
                         OR EXISTS (SELECT 1 FROM dataset_grants g
                                    WHERE g.dataset_id = d.id AND g.user_id = u.id)
                      )
                    ORDER BY u.id
                    """
                ).bindparams(did=dataset_id)
            )
        ).scalars()
        return list(rows)


class SqlAlchemyGrantRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def grant(self, user_id: int, dataset_id: int) -> None:
        async with self.session.begin():
            await self.session.execute(
                text(
                    "INSERT INTO dataset_grants (user_id, dataset_id) VALUES (:u, :d) "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(u=user_id, d=dataset_id)
            )

    async def revoke(self, user_id: int, dataset_id: int) -> int:
        async with self.session.begin():
            result = await self.session.execute(
                text(
                    "DELETE FROM dataset_grants WHERE user_id = :u AND dataset_id = :d"
                ).bindparams(u=user_id, d=dataset_id)
            )
        return result.rowcount or 0


class SqlAlchemyDeliveryLog:
    """Writes the audit rows, and deliberately does NOT open its own transaction.

    The use case has already read from this session, so an implicit transaction
    is open; `async with session.begin()` here raises "a transaction is already
    begun". That error is a design signal, not an inconvenience: a repository
    that opens its own transaction has decided the caller's transaction
    boundary for it, and a caller that wanted the audit rows and the fan-out to
    commit together can no longer have that.

    So: execute, and let the caller commit. One `executemany` rather than a
    loop, because N round trips inside a request is N chances to be slow.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record_many(self, rows: list[dict]) -> None:
        if not rows:
            return
        await self.session.execute(
            text(
                "INSERT INTO feed_deliveries (event_id, user_id, dataset_id, reason) "
                "VALUES (:e, :u, :d, :r) ON CONFLICT DO NOTHING"
            ),
            rows,
        )
        await self.session.commit()
