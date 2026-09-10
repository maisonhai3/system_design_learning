"""Dumb pub/sub broker. Knows nothing about orders or payments.
Its only job: remember who subscribed to what, and fan events out."""
import asyncio, json

subs = {}   # topic -> set of writers


async def handle(reader, writer):
    mine = set()
    try:
        while line := await reader.readline():
            msg = json.loads(line)
            if msg["type"] == "sub":
                subs.setdefault(msg["topic"], set()).add(writer)
                mine.add(msg["topic"])
            elif msg["type"] == "pub":
                ev = msg["event"]
                targets = subs.get(ev["topic"], set()) | subs.get("*", set())
                data = (json.dumps(ev) + "\n").encode()
                for w in targets:
                    w.write(data)
                await asyncio.gather(*(w.drain() for w in targets))
    finally:
        for t in mine:
            subs[t].discard(writer)
        writer.close()


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 9999)
    print("         [   broker] up on 127.0.0.1:9999", flush=True)
    async with server:
        await server.serve_forever()

asyncio.run(main())
