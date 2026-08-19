import uuid

import pytest

from app import db, mq, outbox, redis_client


async def test_db_stubs_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        await db.create_enrollment_request(session=None, student_id=1, course_id=1)
    with pytest.raises(NotImplementedError):
        await db.get_enrollment_request(session=None, request_id=uuid.uuid4())
    with pytest.raises(NotImplementedError):
        await db.try_enroll_student(session=None, request_id=uuid.uuid4(), student_id=1, course_id=1)
    with pytest.raises(NotImplementedError):
        await db.mark_request_status(session=None, request_id=uuid.uuid4(), status=None)


async def test_redis_stubs_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        await redis_client.cache_status(uuid.uuid4(), "pending", 60)
    with pytest.raises(NotImplementedError):
        await redis_client.get_cached_status(uuid.uuid4())
    with pytest.raises(NotImplementedError):
        await redis_client.publish_status(uuid.uuid4(), "pending")
    with pytest.raises(NotImplementedError):
        async for _ in redis_client.subscribe_status(uuid.uuid4()):
            pass


async def test_mq_stubs_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        await mq.publish_request(uuid.uuid4())
    with pytest.raises(NotImplementedError):
        await mq.consume_requests(callback=None)


def test_outbox_stubs_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        outbox.spool_write(uuid.uuid4(), {})
    with pytest.raises(NotImplementedError):
        outbox.spool_read_all()
    with pytest.raises(NotImplementedError):
        outbox.spool_remove(uuid.uuid4())
