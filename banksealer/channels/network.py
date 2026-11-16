"""Network channels: TCP JSON-lines listener and HTTP webhook receiver."""
from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .base import Channel


def _records(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("transactions"), list):
        return payload["transactions"]
    return [payload]


class TCPChannel(Channel):
    """Listens on host:port; every line received is a JSON transaction (or a JSON list)."""

    type_name = "tcp"
    description = "TCP listener, one JSON transaction per line"
    default_options = {"host": "127.0.0.1", "port": 9009}

    def run(self) -> None:
        channel = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    if channel._stop.is_set():
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        for record in _records(json.loads(line)):
                            channel.emit(record)
                    except json.JSONDecodeError as exc:
                        channel.last_error = f"invalid JSON line: {exc}"
                        channel.preparer.stats["rejected"] += 1

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer((self.options["host"], int(self.options["port"])), Handler)
        self.server.daemon_threads = True
        self.server.serve_forever(poll_interval=0.3)

    def on_stop(self) -> None:
        server = getattr(self, "server", None)
        if server:
            threading.Thread(target=server.shutdown, daemon=True).start()
            server.server_close()


class HTTPChannel(Channel):
    """HTTP webhook: POST a JSON transaction, a JSON list, or {"transactions": [...]}."""

    type_name = "http"
    description = "HTTP webhook receiver (POST JSON)"
    default_options = {"host": "127.0.0.1", "port": 8088, "path": "/transactions"}

    def run(self) -> None:
        channel = self
        path = self.options.get("path", "/transactions")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the console quiet
                pass

            def _reply(self, code: int, body: dict):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply(200, {"channel": channel.name, "status": channel.status, "received": channel.received})

            def do_POST(self):
                if self.path.split("?")[0] != path:
                    return self._reply(404, {"error": f"POST to {path}"})
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    records = _records(json.loads(self.rfile.read(length) or b"null"))
                except json.JSONDecodeError as exc:
                    return self._reply(400, {"error": f"invalid JSON: {exc}"})
                for record in records:
                    channel.emit(record)
                self._reply(202, {"accepted": len(records)})

        self.server = ThreadingHTTPServer((self.options["host"], int(self.options["port"])), Handler)
        self.server.daemon_threads = True
        self.server.serve_forever(poll_interval=0.3)

    def on_stop(self) -> None:
        server = getattr(self, "server", None)
        if server:
            threading.Thread(target=server.shutdown, daemon=True).start()
            server.server_close()
