"""Authenticated loopback HTTP server: state, commands, imagery, and shutdown."""
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

def make_server(backend, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Tile query strings carry the session token: never log them.

        def send(self, status, body, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, allow_nan=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store" if content_type == "application/json" else "private, max-age=86400")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Clients can cancel obsolete imagery requests.

        def authorized(self, query, *, tiles=False):
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if tiles:
                supplied = parse_qs(query).get("token", [""])[0]
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                self.send(401, {"error": "Unauthorized"})
                return False
            return True

        def do_GET(self):
            url = urlsplit(self.path)
            tile = url.path.startswith("/tiles/")
            if not self.authorized(url.query, tiles=tile):
                return
            try:
                if url.path == "/state":
                    self.send(200, backend.state())
                elif tile:
                    parts = url.path.split("/")
                    if len(parts) != 6 or not parts[5].endswith(".png"):
                        raise ValueError("Invalid tile URL")
                    self.send(200, backend.tiles.png(parts[2], int(parts[3]), int(parts[4]),
                                                    int(parts[5][:-4])), "image/png")
                else:
                    self.send(404, {"error": "Not found"})
            except (ValueError, OSError) as exc:
                self.send(400 if isinstance(exc, ValueError) else 503, {"error": str(exc)})

        def do_POST(self):
            if not self.authorized(""):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object")
                if self.path == "/command":
                    self.send(200, backend.command(data.get("action"), data))
                elif self.path == "/prepare-quit":
                    self.send(200, backend.prepare_quit())
                elif self.path == "/shutdown":
                    if backend.request_shutdown(confirmed_armed=data.get("confirmed_armed") is True):
                        self.send(200, {"ok": True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    else:
                        self.send(409, {"error": "Vehicle is armed. Quit confirmation required.", "armed": True})
                else:
                    self.send(404, {"error": "Not found"})
            except TimeoutError as exc:
                self.send(504, {"error": str(exc)})
            except RuntimeError as exc:
                self.send(503, {"error": str(exc)})
            except (ValueError, TypeError, OSError) as exc:
                self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server
