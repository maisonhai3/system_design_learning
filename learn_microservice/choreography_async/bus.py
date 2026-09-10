"""Tiny client library every service uses to talk to the broker.
Protocol: newline-delimited JSON over TCP. Nothing clever on purpose."""
import asyncio, json, time, uuid

BROKER = ("127.0.0.1", 9999)


def log(service, msg):
    print(f"{time.strftime('%H:%M:%S')} [{service:>9}] {msg}", flush=True)


class Bus:
    def __init__(self, service):
        self.service = service
        self.handlers = {}          # topic -> [async fn]
        self.reader = self.writer = None

    async def _send(self, msg):
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()

    def on(self, topic):
        def deco(fn):
            self.handlers.setdefault(topic, []).append(fn)
            return fn
        return deco

    async def connect(self):
        while True:                              # broker may start a beat later than us
            try:
                self.reader, self.writer = await asyncio.open_connection(*BROKER)
                break
            except OSError:
                await asyncio.sleep(0.3)
        for topic in self.handlers:
            await self._send({"type": "sub", "topic": topic})
        if self.handlers:
            log(self.service, f"listening for {sorted(self.handlers)}")

    async def publish(self, topic, **payload):
        event = {"id": uuid.uuid4().hex[:8], "topic": topic, "source": self.service,
                 "ts": time.time(), **payload}
        log(self.service, f"emit {topic} {payload}")
        await self._send({"type": "pub", "event": event})

    async def run(self):
        await self.connect()
        while line := await self.reader.readline():
            event = json.loads(line)
            for fn in self.handlers.get(event["topic"], []) + self.handlers.get("*", []):
                await fn(event)
