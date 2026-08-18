import asyncio
import uuid
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import db, mq, redis_client
from app.config import settings
from app.database import AsyncSessionLocal


async def handle_message(
    session_factory: async_sessionmaker[AsyncSession],
    request_id: uuid.UUID,
    ack: Callable[[], Awaitable[None]],
    nack: Callable[[], Awaitable[None]],
) -> None:
    async with session_factory() as session:
        try:
            req = await db.get_enrollment_request(session, request_id)
            if req is None:
                await nack()
                return
            status = await db.try_enroll_student(session, request_id, req.student_id, req.course_id)
            await db.mark_request_status(session, request_id, status)
            await session.commit()
        except Exception:
            await session.rollback()
            await nack()
            return

    try:
        await redis_client.cache_status(request_id, status.value, settings.status_cache_ttl_seconds)
        await redis_client.publish_status(request_id, status.value)
    except Exception:
        pass
    await ack()


async def run_worker(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async def callback(request_id: uuid.UUID, ack, nack) -> None:
        await handle_message(session_factory, request_id, ack, nack)

    await mq.consume_requests(callback)


if __name__ == "__main__":
    asyncio.run(run_worker(AsyncSessionLocal))
