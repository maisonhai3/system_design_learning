import asyncio

from app import mq, outbox
from app.config import settings


async def relay_once() -> int:
    relayed = 0
    for request_id, _ in outbox.spool_read_all():
        published = await mq.publish_request(request_id)
        if published:
            outbox.spool_remove(request_id)
            relayed += 1
    return relayed


async def run_relay(interval_seconds: float) -> None:
    while True:
        await relay_once()
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    asyncio.run(run_relay(settings.poll_interval_seconds))
