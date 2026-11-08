"""Hub HTTP shell. The only file that touches sockets.

Everything below this takes a DB connection and returns plain values, so the
test suite can exercise the whole API without ever opening a socket."""
import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import api, auth
from .migrations import connect, migrate

_DB_PATH = None
_local = threading.local()
_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
           "/app.js": ("app.js", "text/javascript; charset=utf-8"),
           "/style.css": ("style.css", "text/css; charset=utf-8")}


def _conn():
    """One SQLite connection per thread: ThreadingHTTPServer hands each
    request a worker thread, and sqlite objects must not cross threads.
    WAL mode is set in migrations.connect(), and migrate() is safe to
    run from every opener, so concurrent threads don't fight over it."""
    c = getattr(_local, "con", None)
    if c is None:
        c = connect(_DB_PATH)
        _local.con = c
    return c


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _principal(self):
        authh = self.headers.get("Authorization", "")
        token = authh[7:] if authh.startswith("Bearer ") else None
        return auth.authenticate(_conn(), token)

    def _dispatch(self, m):
        body = None
        if m == "POST":
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        status, payload = api.handle(_conn(), self._principal(), m, self.path, body)
        self._send(status, payload)

    def do_GET(self):
        static = _STATIC.get(self.path.split("?")[0])
        if static:
            try:
                with open(os.path.join(_WEB, static[0]), "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, {"error": {"code": "not_found", "path": self.path}})
            self.send_response(200)
            self.send_header("Content-Type", static[1])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._send(405, {"error": {"code": "method_not_allowed",
                                   "message": "hub is append-only by design"}})

    def log_message(self, *args):
        pass


def serve(port, db, host="127.0.0.1"):
    global _DB_PATH
    _DB_PATH = db
    con = connect(db)
    migrate(con)  # safe to re-run; each thread then opens the same WAL db
    con.close()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--db", default="hub.db")
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to serve the LAN (home Wi-Fi only; no TLS)")
    a = ap.parse_args()
    serve(a.port, a.db, a.host)
