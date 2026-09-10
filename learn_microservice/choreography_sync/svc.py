"""Tiny HTTP helper every service imports. stdlib only."""
import json, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(service, msg):
    print(f"{time.strftime('%H:%M:%S')} [{service:>9}] {msg}", flush=True)


class Downstream(Exception):
    """The service we called said no, or didn't answer at all."""
    def __init__(self, status, body):
        super().__init__(status, body)
        self.status, self.body = status, body


def call(service, url, **payload):
    """Synchronous request/reply. We BLOCK here until the other side answers."""
    log(service, f"→ POST {url} {payload}")
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:                  # got an answer, and it was "no"
        body = json.load(e)
        log(service, f"← {e.code} {body}")
        raise Downstream(e.code, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:   # got no answer at all
        log(service, f"← {url} UNREACHABLE ({getattr(e, 'reason', e)})")
        raise Downstream(503, {"error": f"{url} unreachable"})
    log(service, f"← 200 {body}")
    return body


def serve(service, port, routes):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            handler = routes.get(self.path)
            if handler is None:
                status, out = 404, {"error": "no such route"}
            else:
                try:
                    status, out = handler(body)
                except Downstream as d:                   # unhandled downstream failure bubbles up
                    status, out = 502, {"error": "downstream failed", "cause": d.body}
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):      # silence default access log
            pass

    log(service, f"serving on :{port} {list(routes)}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
