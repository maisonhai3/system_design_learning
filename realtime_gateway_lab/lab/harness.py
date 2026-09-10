"""Shared plumbing for the scenario runners.

Same contract as this repo's redis_cache_lab — `broke()` asserts the anomaly
really reproduced, `held()` asserts the fix really worked, and `finish()` turns
those into an exit code — because a lab that quietly stops demonstrating its own
bug is worse than no lab: you would walk into the interview still believing it.

What is different here is the actors. This lab's failures happen between four
processes (client, gateway, authorization service, upstream) rather than
between two stores, so the primitives are an HTTP actor that carries a token
and an SSE session that can be cut off mid-stream and resumed — because
"reconnect and do not lose events" is not a property you can assert about a
single request.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import re
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field

import httpx
import psycopg
import redis

LAB_ROOT = pathlib.Path(__file__).resolve().parents[1]

DSN = os.environ.get("LAB_DSN", "postgresql://lab:lab@localhost:5437/lab")
REDIS_URL = os.environ.get("LAB_REDIS_URL", "redis://localhost:6381/0")
GATEWAY = f"http://localhost:{os.environ.get('GATEWAY_PORT', '8090')}"
DIRECT = f"http://localhost:{os.environ.get('API_DIRECT_PORT', '8093')}"
SIGNED = f"http://localhost:{os.environ.get('API_SIGNED_PORT', '8096')}"
NGINX = f"http://localhost:{os.environ.get('NGINX_PORT', '8092')}"
AUTHZ = f"http://localhost:{os.environ.get('AUTHZ_PORT', '8094')}"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


BOLD = lambda s: _c("1", s)
DIM = lambda s: _c("2", s)
RED = lambda s: _c("31", s)
GREEN = lambda s: _c("32", s)
YELLOW = lambda s: _c("33", s)
CYAN = lambda s: _c("36", s)

_COLORS = ["36", "35", "33", "34", "32"]
_assigned: dict[str, str] = {}
_print_lock = threading.Lock()


def _color_for(name: str) -> str:
    if name not in _assigned:
        _assigned[name] = _COLORS[len(_assigned) % len(_COLORS)]
    return _assigned[name]


def _tag(name: str) -> str:
    return _c(_color_for(name), f"{name:>7}")


def emit(name: str, text: str, trailer: str = "") -> None:
    line = f"  {_tag(name)} {DIM('│')} {text}"
    if trailer:
        line += f"  {trailer}"
    with _print_lock:
        print(line, flush=True)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

sys.path.insert(0, str(LAB_ROOT / "gateway"))
from tokens import mint  # noqa: E402


def token_for(who: str, ttl: int = 3600, jti: str | None = None) -> str:
    return mint(who, ttl, jti)


def jti_of(token: str) -> str:
    """Pull the token id out without verifying. Fine here: we minted it.

    Worth knowing that this is trivial, because it is why a JWT is not a
    secret-keeping device. Anyone holding the token can read every claim in it.
    Signing proves the claims were not EDITED; it does nothing to hide them.
    """
    import base64

    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["jti"]


# ---------------------------------------------------------------------------
# HTTP actors
# ---------------------------------------------------------------------------


class Actor:
    """One caller, with a token and a base URL. Every request is traced."""

    def __init__(self, name: str, who: str | None = None, base: str = GATEWAY, ttl: int = 3600):
        self.name = name
        self.who = who
        self.base = base
        self.token = token_for(who, ttl) if who else None
        self.client = httpx.Client(timeout=30.0)

    def _headers(self, extra: dict | None) -> dict:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra or {})
        return headers

    def request(self, method: str, path: str, headers: dict | None = None, **kw) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base}{path}"
        started = time.perf_counter()
        response = self.client.request(method, url, headers=self._headers(headers), **kw)
        ms = (time.perf_counter() - started) * 1000
        colour = GREEN if response.status_code < 400 else RED
        forged = [k for k in (headers or {}) if k.lower().startswith("x-auth")]
        note = DIM(f"  +forged {','.join(forged)}") if forged else ""
        emit(
            self.name,
            f"{method} {url}{note}",
            colour(f"→ {response.status_code}") + DIM(f" {ms:.0f}ms"),
        )
        return response

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def json(self, method: str, path: str, **kw):
        response = self.request(method, path, **kw)
        try:
            return response.json()
        except Exception:
            return {"_raw": response.text, "_status": response.status_code}

    def say(self, text: str) -> None:
        emit(self.name, DIM(f"# {text}"))

    def close(self):
        self.client.close()


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


@dataclass
class SseFrame:
    event: str = "message"
    data: str = ""
    id: str | None = None
    comment: str | None = None
    received_at: float = field(default_factory=time.monotonic)

    @property
    def payload(self) -> dict:
        try:
            return json.loads(self.data)
        except Exception:
            return {}


class SseSession:
    """One long-lived SSE connection, driven from a background thread.

    Deliberately parses the wire format by hand rather than using a client
    library, because the two things that go wrong are invisible behind one:

      * a frame is not dispatched until the parser sees a BLANK LINE, so a
        server that forgets the second "\\n" produces a stream that connects,
        transfers bytes, and never fires a single event;
      * `id:` is sticky — the last id seen becomes Last-Event-ID for the NEXT
        connection, which is the entire resume mechanism, and it is easy to
        implement a server that never sends one and never notices.
    """

    def __init__(
        self,
        name: str,
        url: str,
        token: str | None = None,
        last_event_id: str | None = None,
        headers: dict | None = None,
        trace: bool = True,
    ):
        self.name = name
        self.url = url
        self.token = token
        self.last_event_id = last_event_id
        self.extra_headers = headers or {}
        self.trace = trace
        self.frames: list[SseFrame] = []
        self.comments: list[SseFrame] = []
        self.error: Exception | None = None
        self.opened_at: float | None = None
        self.first_frame_at: float | None = None
        self.status_code: int | None = None
        self.closed_at: float | None = None
        self._queue: queue.Queue[SseFrame] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None

    def open(self) -> "SseSession":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _headers(self) -> dict:
        headers = {"Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.last_event_id:
            # The header the browser sets by itself on an EventSource reconnect.
            # A fetch-based client must set it explicitly, and forgetting to is
            # the single most common way a "resumable" feed silently is not.
            headers["Last-Event-ID"] = self.last_event_id
        headers.update(self.extra_headers)
        return headers

    def _run(self) -> None:
        try:
            self._client = httpx.Client(timeout=httpx.Timeout(None, connect=10.0))
            with self._client as client:
                with client.stream("GET", self.url, headers=self._headers()) as response:
                    self.opened_at = time.monotonic()
                    self.status_code = response.status_code
                    if self.trace:
                        emit(
                            self.name,
                            f"GET {self.url}"
                            + (DIM(f"  Last-Event-ID: {self.last_event_id}") if self.last_event_id else ""),
                            GREEN(f"→ {response.status_code}") + DIM(" streaming"),
                        )
                    if response.status_code >= 400:
                        self.error = RuntimeError(f"HTTP {response.status_code}")
                        return
                    self._consume(response)
        except Exception as exc:  # noqa: BLE001 - surfaced via .error / .why_empty()
            if not self._stop.is_set():
                self.error = exc
        finally:
            self.closed_at = time.monotonic()

    def _consume(self, response: httpx.Response) -> None:
        event, data_lines, event_id = "message", [], None
        for line in response.iter_lines():
            if self._stop.is_set():
                return
            if line.startswith(":"):
                frame = SseFrame(comment=line)
                self.comments.append(frame)
                if self.trace:
                    emit(self.name, DIM(f"{line.strip()}"), DIM("(keep-alive)"))
                continue
            if line == "":
                # The blank line is the dispatch. Everything before it was one
                # frame; nothing is delivered without it.
                if data_lines or event_id:
                    frame = SseFrame(event=event, data="\n".join(data_lines), id=event_id)
                    self._record(frame)
                event, data_lines, event_id = "message", [], None
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data_lines.append(value)
            elif field == "id":
                event_id = value
            elif field == "retry":
                if self.trace:
                    emit(self.name, DIM(f"retry: {value}ms"), DIM("(server sets the backoff)"))

    def _record(self, frame: SseFrame) -> None:
        if frame.id:
            self.last_event_id = frame.id
        if self.first_frame_at is None:
            self.first_frame_at = time.monotonic()
        self.frames.append(frame)
        self._queue.put(frame)
        if self.trace and frame.event not in ("hello",):
            payload = frame.payload
            label = payload.get("dataset_name") or frame.data[:48]
            emit(self.name, CYAN(f"{frame.event}"), DIM(f"id={frame.id} {label}"))

    def wait_for(self, count: int, timeout: float = 15.0, event: str | None = None) -> list[SseFrame]:
        """Block until `count` frames of the given kind have arrived."""
        deadline = time.monotonic() + timeout
        got = [f for f in self.frames if event is None or f.event == event]
        while len(got) < count and time.monotonic() < deadline:
            try:
                self._queue.get(timeout=0.1)
            except queue.Empty:
                pass
            got = [f for f in self.frames if event is None or f.event == event]
        return got

    def why_empty(self) -> str:
        """Why no frames arrived — the difference between a lab and a shrug.

        "no event" is not a finding, it is the absence of one. It could be a
        403 from the gateway, a proxy that is not running, a connection the
        proxy closed, or a stream that was genuinely healthy and idle. Those
        need four different fixes, so a measurement that cannot tell them
        apart is a measurement that wastes your afternoon.
        """
        if self.error is not None:
            return f"{type(self.error).__name__}: {str(self.error)[:70] or 'connection failed'}"
        if self.status_code is None:
            # No response headers at all. Two very different causes, and the
            # thread tells them apart: still running means the socket is open
            # and something upstream is withholding the response (a buffering
            # or compressing proxy holds back even the headers); finished means
            # the connection never came up.
            if self._thread is not None and self._thread.is_alive():
                return "no response headers at all — a proxy is withholding the whole response"
            return "never connected — nothing listening, or the connection was refused"
        if self.status_code >= 400:
            return f"HTTP {self.status_code} — authentication or routing, not buffering"
        if self.closed_at and self.opened_at and not self.frames:
            return f"connected {self.status_code}, closed after {self.closed_at - self.opened_at:.1f}s with no frames"
        if self.comments:
            return f"connected {self.status_code}, {len(self.comments)} keep-alive(s), no events"
        return f"connected {self.status_code}, nothing arrived at all"

    def data_frames(self) -> list[SseFrame]:
        """Everything except the connection's own housekeeping."""
        return [f for f in self.frames if f.event not in ("hello",)]

    def wait_closed(self, timeout: float = 30.0) -> float | None:
        """Seconds until the server (or a proxy) closed the connection.

        Returns None if it was still open when we gave up — which is the
        answer you want for a stream, and the one you will not get through a
        proxy whose idle timeout is shorter than your keep-alive interval.
        """
        started = time.monotonic()
        if self._thread is None:
            return None
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            return None
        return time.monotonic() - started

    def disconnect(self) -> "SseSession":
        """Cut the connection the way a lift does: no goodbye.

        Closing the CLIENT, not just setting a flag. A flag only stops this
        side reading; the socket stays open, so the server keeps the request —
        and everything the request holds — alive. That distinction is exactly
        what scenario 06 is about, and getting it wrong here first is how this
        harness learned it.
        """
        if self.trace:
            emit(self.name, DIM("*** connection dropped ***"), YELLOW(f"last id {self.last_event_id}"))
        self._stop.set()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        return self

    def close(self) -> None:
        self.disconnect()


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


def redis_client() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def db() -> psycopg.Connection:
    return psycopg.connect(DSN, autocommit=True)


def reset() -> None:
    """Re-apply schema + seed, delete every feed stream. Runners call this first.

    Note what it does NOT delete: the gateway's `authz:*` decision cache. A
    reset that silently emptied it would reset the very thing scenario 04 is
    measuring, and a lab that cleans up state you were about to observe is a
    lab that proves whatever it likes.
    """
    with db() as conn:
        for name in ("01_schema.sql", "02_seed.sql"):
            conn.execute((LAB_ROOT / "schema" / name).read_text())
    client = redis_client()
    for pattern in ("feed:*",):
        keys = list(client.scan_iter(pattern, count=500))
        if keys:
            client.delete(*keys)
    client.close()


def stream_contents(stream: str) -> list[tuple[str, dict]]:
    client = redis_client()
    try:
        return client.xrange(stream)
    finally:
        client.close()


def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=LAB_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def require_stack() -> None:
    """Fail with a useful message rather than a connection error."""
    try:
        httpx.get(f"{GATEWAY}/healthz", timeout=3.0).raise_for_status()
    except Exception:
        print(RED("The stack is not up. Start it with:  ./lab.sh up"), file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class Lab:
    def __init__(self, title: str, subtitle: str = "", needs_stack: bool = True):
        self.title = title
        self.failures: list[str] = []
        self.passes = 0
        self._actors: list[Actor] = []
        self._streams: list[SseSession] = []
        if needs_stack:
            require_stack()
        print()
        print(BOLD(f"┏━ {title}"))
        if subtitle:
            print(DIM(f"┃  {subtitle}"))
        print()

    def actor(self, name: str, who: str | None = None, base: str = GATEWAY, ttl: int = 3600) -> Actor:
        a = Actor(name, who, base, ttl)
        self._actors.append(a)
        return a

    def stream(self, name: str, url: str, **kw) -> SseSession:
        s = SseSession(name, url, **kw)
        self._streams.append(s)
        return s

    def section(self, title: str) -> None:
        print()
        print(BOLD(f"── {title} " + "─" * max(0, 62 - len(title))))

    def note(self, text: str) -> None:
        for line in textwrap.dedent(text).strip("\n").splitlines():
            print(DIM(f"     {line}"))

    def measure(self, label: str, value: str) -> None:
        print(f"     {BOLD(label)}  {value}")

    def _record(self, ok: bool, kind: str, description: str, detail: str) -> None:
        if ok:
            self.passes += 1
            print(f"  {GREEN('✓')} {kind}: {description}")
        else:
            self.failures.append(f"{kind}: {description}")
            print(f"  {RED('✗')} {kind}: {description}")
        if detail:
            for line in textwrap.dedent(detail).strip("\n").splitlines():
                print(DIM(f"      {line}"))

    def broke(self, condition, description: str, detail: str = "") -> bool:
        self._record(bool(condition), "ANOMALY REPRODUCED", description, detail)
        return bool(condition)

    def held(self, condition, description: str, detail: str = "") -> bool:
        self._record(bool(condition), "FIX HELD", description, detail)
        return bool(condition)

    def require(self, condition, description: str, detail: str = "") -> None:
        """Assert something the scenario needs before it can measure anything.

        Use it wherever the SHAPE of a remote response is about to be assumed —
        `result["event_id"]`, `payload["datasets"]`. When the call actually
        returned a 403, indexing it raises a KeyError, and a traceback is a
        strictly worse bug report than a sentence: it points at the line that
        read the value instead of the request that failed.

        The rule this encodes: a measurement derived from a remote call must
        handle that call having failed. Locally-computed values do not need
        this; anything that crossed a network does.
        """
        if not condition:
            self.precondition_failed(description, detail)

    def precondition_failed(self, description: str, detail: str = "") -> None:
        """The lab cannot run here, and that is not the same as a claim failing.

        Exit code 3, distinct from 1 (a claim did not hold) and 2 (the stack is
        not up), so `run-all` and CI can tell "your environment is wrong" from
        "your system is wrong". Conflating those two is how a broken
        environment gets read as a broken fix — you go looking at the code
        while the actual answer is that a container is not running.
        """
        print()
        print(YELLOW(BOLD(f"  ⚠ CANNOT RUN: {description}")))
        if detail:
            for line in textwrap.dedent(detail).strip("\n").splitlines():
                print(DIM(f"      {line}"))
        for stream in self._streams:
            stream.close()
        for actor in self._actors:
            actor.close()
        print()
        print(YELLOW(BOLD(f"┗━ {self.title}: precondition not met; no claims were tested")))
        sys.exit(3)

    def takeaway(self, text: str) -> None:
        print()
        print(BOLD("  Say this out loud:"))
        for line in textwrap.dedent(text).strip("\n").splitlines():
            print(f"    {line}")

    def finish(self) -> None:
        for s in self._streams:
            s.close()
        for a in self._actors:
            a.close()
        print()
        if self.failures:
            print(RED(BOLD(f"┗━ {self.title}: {len(self.failures)} claim(s) did not hold")))
            for f in self.failures:
                print(RED(f"     - {f}"))
            print(DIM("   The lab is no longer demonstrating what it says it demonstrates."))
            sys.exit(1)
        print(GREEN(BOLD(f"┗━ {self.title}: {self.passes}/{self.passes} claims held")))
        sys.exit(0)
