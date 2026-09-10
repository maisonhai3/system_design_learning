# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "psycopg[binary]", "redis"]
# ///
"""Scenario 05 — everything that breaks SSE breaks in the proxy, not your code."""

import os
import pathlib
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import GATEWAY, NGINX, Lab, reset, token_for  # noqa: E402

ALICE = token_for("alice")
PUBLIC = 12

lab = Lab(
    "05 — SSE THROUGH THE GATEWAY",
    "The application is perfect. The user sees a page that never loads.",
)
reset()

publisher = lab.actor("admin", "alice")


@dataclass
class Probe:
    """A time-to-first-event measurement, or the reason there wasn't one.

    Returning a value-or-None and formatting it later is how this scenario
    crashed with `unsupported format string passed to NoneType.__format__` —
    a traceback that hides the actual problem, which was that nothing was
    reaching nginx at all. So the measurement carries its own rendering and
    its own explanation, and there is no code path that formats a missing
    number.
    """

    seconds: float | None
    reason: str = ""

    @property
    def arrived(self) -> bool:
        return self.seconds is not None

    def __str__(self) -> str:
        return f"{self.seconds:.2f}s" if self.arrived else f"no event — {self.reason}"


def time_to_first_event(url: str, label: str, headers: dict | None = None, wait: float = 12.0) -> Probe:
    """Open a stream, publish into it, and time the first frame that arrives."""
    stream = lab.stream(label, url, token=ALICE, headers=headers, trace=False).open()
    time.sleep(0.6)  # let the connection establish and park in XREAD BLOCK
    started = time.monotonic()
    publisher.request("POST", f"/events/{PUBLIC}?mode=fanout")
    frames = stream.wait_for(1, timeout=wait, event="dataset.processed")
    probe = (
        Probe(time.monotonic() - started)
        if frames
        else Probe(None, stream.why_empty())
    )
    stream.disconnect()
    return probe


# ---------------------------------------------------------------------------
lab.section("The anomaly: it works in curl and fails in every browser")
lab.note(
    """
    nginx's /buffered/ location is configured the way a hardening guide would
    leave it: `proxy_buffering on` (the default) and `gzip on` for text/*.
    `text/event-stream` is text/*.

    Two identical requests. The only difference is whether the client says it
    accepts gzip — which curl does not by default, and every browser does.
    """
)

plain = time_to_first_event(
    f"{NGINX}/buffered/feed?block_ms=1000&no_accel_header=true",
    "no-gzip",
    headers={"Accept-Encoding": "identity"},
)
gzipped = time_to_first_event(
    f"{NGINX}/buffered/feed?block_ms=1000&no_accel_header=true",
    "gzip",
    headers={"Accept-Encoding": "gzip"},
)

lab.measure("Accept-Encoding: identity →", str(plain))
lab.measure("Accept-Encoding: gzip     →", str(gzipped))

# The identity request is the CONTROL. If it fails, the comparison below is
# meaningless — and reporting "the anomaly did not reproduce" would send you
# looking at gzip when the real problem is that nginx is not serving at all.
# A precondition that fails is a different kind of failure and deserves to say
# so, loudly, once, with the things to check.
if not plain.arrived:
    lab.precondition_failed(
        "nginx is not delivering this stream at all, so there is nothing to compare",
        f"""
        The control request — the one WITHOUT gzip — got: {plain.reason}

        This scenario compares two requests through nginx that differ only in
        Accept-Encoding. Both failed, so the difference cannot be measured.
        Nothing here is about buffering yet. Check, in this order:

          ./lab.sh logs nginx           is it running, or did it exit on a
                                        config error? `up` does not wait for
                                        nginx, so a dead one is quiet.
          curl -i localhost:{os.environ.get('NGINX_PORT', '8092')}/buffered/whoami \
               -H "Authorization: Bearer $(./lab.sh token alice)"
                                        401/403 means auth_request, not buffering.
          docker compose restart nginx  nginx reads its config once, at start.
                                        A bind-mounted config you pulled after
                                        the container was created is NOT loaded
                                        until the process restarts, and
                                        `docker compose up -d` cannot tell.
        """,
    )

lab.broke(
    plain.arrived and not gzipped.arrived,
    "the same endpoint delivers instantly to curl and never to a gzip-capable client",
    """
    The compressor works in blocks. It will not emit anything until it has
    enough input to be worth compressing, so 200-byte SSE frames sit in the
    deflate window while the user watches a spinner. Nothing times out, nothing
    errors, no log line is written: the request is open and healthy and
    delivering nothing.

    Read the trigger again, because it is the reason this reaches production:
    it is the CLIENT's Accept-Encoding header. Your curl test passes. Your
    integration test — if it uses a plain HTTP client with compression off —
    passes. It fails only for real browsers, which is to say only for users.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Fix 1: the application says 'do not transform this'")

rescued = time_to_first_event(
    f"{NGINX}/buffered/feed?block_ms=1000",  # the app now sends X-Accel-Buffering: no
    "accel-no",
    headers={"Accept-Encoding": "gzip"},
)
lab.measure("with X-Accel-Buffering: no →", str(rescued))

lab.held(
    rescued.arrived and rescued.seconds < 2.0,
    f"the same location now delivers in {rescued}, with no nginx change at all",
    """
    Two response headers do this, and they are worth sending from every
    streaming endpoint you write:

      X-Accel-Buffering: no        nginx-specific, disables buffering (and with
                                   it the compression) for THIS response.
      Cache-Control: no-transform  the standards-track one. Tells any
                                   intermediary it may not re-encode the body.

    Why send them when you also control the proxy config? Because you often do
    not. The CDN, the corporate proxy, the ingress controller someone else
    manages, the service mesh added next quarter — a response header travels
    with the response and protects it in paths you have never seen.
    """,
)

streamed = time_to_first_event(
    f"{NGINX}/streamed/feed?block_ms=1000&no_accel_header=true",
    "streamed",
    headers={"Accept-Encoding": "gzip"},
)
traefik = time_to_first_event(f"{GATEWAY}/feed?block_ms=1000", "traefik")

lab.measure("nginx /streamed/ (buffering off) →", str(streamed))
lab.measure("traefik (no buffering by default) →", str(traefik))

lab.held(
    streamed.arrived and traefik.arrived,
    "fixing the proxy works too — and note Traefik needed no fix",
    """
    Traefik does not buffer responses, so SSE works through it out of the box.
    That is a real difference and it is not the lesson: the lesson is that
    "works out of the box" is a property of a default, and defaults change,
    get overridden by a middleware someone adds, and differ between the proxy
    you develop against and the one in front of production.

    Send the headers anyway.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The anomaly: an idle stream is an invisible stream")
lab.note(
    """
    A feed is idle most of the time — that is the whole point of a push
    channel. Every proxy in the path has an idle timeout, and to a proxy,
    "healthy long-lived connection" and "leaked connection" look identical.

    nginx /buffered/ has proxy_read_timeout 5s. The application is told to
    block for 10s between keep-alives.
    """
)

idle = lab.stream(
    "idle", f"{NGINX}/buffered/feed?block_ms=10000&no_accel_header=true",
    token=ALICE, headers={"Accept-Encoding": "identity"}, trace=False,
).open()
closed_after = idle.wait_closed(timeout=20)

lab.measure("keep-alive interval:", "10s")
lab.measure("proxy_read_timeout:", "5s")
lab.measure("connection survived:", f"{closed_after:.1f}s" if closed_after else ">20s (never closed)")

lab.broke(
    closed_after is not None and closed_after < 8,
    f"the proxy closed the stream after {closed_after:.1f}s of quiet"
    if closed_after is not None
    else "the proxy did NOT close the idle stream (expected it to, at 5s)",
    """
    The client will reconnect — that is what `retry:` is for — so the symptom
    is not an outage. It is a reconnect every five seconds, per user, forever:
    a login storm, an authorization-service load spike, a decision cache
    thrashing, and a graph of "connections per second" nobody can explain.

    Note the shape of the bug. It is a RELATIONSHIP between two numbers that
    live in two different repositories, owned by two different teams: the
    application's keep-alive interval and the proxy's idle timeout. Neither is
    wrong on its own. Nothing validates them together, and nothing ever will.
    Write the relationship down where both teams read it.
    """,
)

healthy = lab.stream(
    "healthy", f"{NGINX}/streamed/feed?block_ms=2000&no_accel_header=true",
    token=ALICE, headers={"Accept-Encoding": "identity"}, trace=False,
).open()
survived = healthy.wait_closed(timeout=8)
keepalives = len(healthy.comments)
healthy.disconnect()

lab.measure("keep-alives received in 8s:", str(keepalives))
lab.held(
    survived is None and keepalives >= 2,
    "with a keep-alive shorter than the timeout, the connection simply stays up",
    """
        : keep-alive

    A comment frame — a line starting with a colon — is the whole mechanism.
    The SSE parser ignores it, so the client never sees an event it has to
    filter out, and every proxy in the path sees traffic and resets its idle
    timer.

    Pick the interval as a fraction of the SHORTEST timeout in the path, not
    of the one you know about. Somewhere between a third and a half is the
    usual choice, and the number you are dividing is whichever proxy you
    forgot: the ingress, the CDN, the corporate MITM, the cloud load balancer
    whose default is 60 seconds.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The checklist, for the endpoint and for the proxy")
lab.note(
    """
    On the response, from the application:
      Content-Type: text/event-stream
      Cache-Control: no-cache, no-transform
      X-Accel-Buffering: no
      a `: keep-alive` comment on every idle tick
      a `retry:` line, so the reconnect interval is yours to choose
      an `id:` on every frame, or none of the resume design works

    On the proxy:
      buffering off, compression off, caching off for this route
      read/idle timeout comfortably longer than the keep-alive interval
      response write timeout disabled — a response that never ends is the point
      HTTP/1.1 to the upstream, and no `Connection: close`

    And one thing that is neither: a rate limiter counts REQUESTS, and one SSE
    request is a thousand events. Limiting streams needs a concurrency cap, not
    a rate limit — which is scenario 06.
    """
)

lab.takeaway(
    """
    "Everything that breaks SSE breaks in the proxy, not in the application.
     The one that reaches production is compression: `gzip on` for text/* also
     matches text/event-stream, the compressor holds small frames until it has
     a block worth emitting, and it only triggers for clients that send
     Accept-Encoding — so it passes in curl and fails in every browser. I send
     `Cache-Control: no-transform` and `X-Accel-Buffering: no` from the
     endpoint, because the response header protects the stream through proxies
     I do not control.

     The second one is the idle timeout: a push channel is quiet by design, and
     to a proxy a healthy idle connection looks exactly like a leaked one. A
     `: keep-alive` comment at a fraction of the shortest timeout in the path
     fixes it — and the bug's real shape is that it is a relationship between
     two numbers in two different repositories that nothing validates together."
    """
)

lab.finish()
