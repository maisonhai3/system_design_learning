"""SQLAlchemy 2.0 async adapters. The only module that knows what a table is.

Note on scope: the schema here is owned by `schema/*.sql` because the lab needs
to reset it in about a second, and the scenarios drive it with raw psycopg. In a
real service this is where Alembic goes — see this repo's
`student_course_enrollment` lab for the Alembic setup this deliberately omits.
"""

from __future__ import annotations

from sqlalchemy import Integer, String, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.entities import Dataset, User


def make_engine(dsn: str):
    # pool_pre_ping because a lab container gets restarted under you and a
    # stale pooled connection surfaces as a confusing error in the middle of a
    # request rather than at connect time.
    return create_async_engine(dsn, pool_pre_ping=True, pool_size=10, max_overflow=20)


def make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: otherwise every attribute access after a commit
    # emits a fresh SELECT, and in async code that is an await you did not
    # write, in a place you cannot see.
    return async_sessionmaker(engine, expire_on_commit=False)


class SqlAlchemyUserRepository:
    """Satisfies UserRepository structurally — it never imports the Protocol."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, user_id: int) -> User | None:
        row = (
            await self.session.execute(
                text(
                    "SELECT id, email, display_name, role, org_id, version "
                    "FROM users WHERE id = :id"
                ).bindparams(id=user_id)
            )
        ).mappings().first()
        return User(**row) if row else None

    async def set_role(self, user_id: int, role: str) -> User | None:
        """One statement, one transaction, and the new version comes back with it.

        RETURNING is doing real work here: without it you need a second SELECT,
        and between the UPDATE and that SELECT another writer can bump the
        version again — so you would cache a value under a version that is not
        the one your write produced.
        """
        async with self.session.begin():
            row = (
                await self.session.execute(
                    text(
                        "UPDATE users SET role = :role WHERE id = :id "
                        "RETURNING id, email, display_name, role, org_id, version"
                    ).bindparams(role=role, id=user_id)
                )
            ).mappings().first()
        return User(**row) if row else None


class SqlAlchemyDatasetRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def visible_to(self, user_id: int, org_id: int) -> list[Dataset]:
        """ABAC in the WHERE clause, not in Python afterwards.

        Filtering after fetching means the restricted rows were on the wire, in
        memory, and one `logger.debug(rows)` away from your log aggregator. The
        database is the last place you can cheaply not-have the data at all.
        """
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT d.id, d.org_id, d.name, d.classification
                    FROM datasets d
                    WHERE d.org_id = :org
                      AND (d.classification IN ('public', 'internal')
                           OR EXISTS (SELECT 1 FROM dataset_grants g
                                      WHERE g.dataset_id = d.id AND g.user_id = :uid))
                    ORDER BY d.id
                    """
                ).bindparams(org=org_id, uid=user_id)
            )
        ).mappings().all()
        return [Dataset(**r) for r in rows]


class SqlAlchemyGrantRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def revoke(self, user_id: int, dataset_id: int) -> int:
        async with self.session.begin():
            result = await self.session.execute(
                text(
                    "DELETE FROM dataset_grants WHERE user_id = :uid AND dataset_id = :did"
                ).bindparams(uid=user_id, did=dataset_id)
            )
        return result.rowcount or 0

    async def grant(self, user_id: int, dataset_id: int) -> None:
        async with self.session.begin():
            await self.session.execute(
                text(
                    "INSERT INTO dataset_grants (user_id, dataset_id) VALUES (:uid, :did) "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(uid=user_id, did=dataset_id)
            )
