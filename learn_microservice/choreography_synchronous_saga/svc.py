"""Tiny HTTP helper every service imports. stdlib only. (Same as choreo_sync.)"""
import json, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(service, msg):
    print(f"{time.strftime('%H:%M:%S')} [{service:>9}] {msg}", flush=True)


class Downstream(Exception):
    def __init__(self, status, body):
        super().__init__(status, body)
        self.status, self.body = status, body


def call(service, url, **payload):
    """Synchronous request/reply. We BLOCK until the other side answers."""
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:                 # answered "no"
        raise Downstream(e.code, json.load(e))
    except (urllib.error.URLError, TimeoutError, OSError) as e:   # no answer
        raise Downstream(503, {"reason": f"{url} unreachable ({getattr(e,'reason',e)})"})


def serve(service, port, routes):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            fn = routes.get(self.path)
            status, out = fn(body) if fn else (404, {"reason": "no route"})
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *a): pass
    log(service, f"serving on :{port} {list(routes)}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
