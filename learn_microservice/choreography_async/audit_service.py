"""Subscribes to everything. Touches nothing. This is how you *see* a
choreography — nobody else has the whole picture."""
import asyncio
from bus import Bus, log

bus = Bus("audit")
META = {"id", "topic", "source", "ts"}


@bus.on("*")
async def tap(ev):
    body = {k: v for k, v in ev.items() if k not in META}
    log("audit", f"{ev['source']:>9} ── {ev['topic']:<16} {body}")


asyncio.run(bus.run())
