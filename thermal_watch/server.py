from __future__ import annotations

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .inputs import SnapshotError, read_csv


STATIC = Path(__file__).parent / "static"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}


class Application:
    def __init__(self, monitor, csv_path=None, notifier=None):
        self.monitor = monitor
        self.csv_path = csv_path
        self.notifier = notifier
        self.mode = "live" if csv_path else "demo"
        self.stopped = threading.Event()
        self.input_error = None
        self.started = time.monotonic()
        self.watermark = max((e["id"] for e in monitor.snapshot()["events"]), default=0)
        self.threads = []

    def poll(self):
        if self.csv_path:
            try:
                read_csv(self.csv_path, self.monitor)
                self.input_error = None
            except SnapshotError:
                self.input_error = "One or more sensors have invalid input. Check the producer."
            except (OSError, ValueError, UnicodeError):
                self.input_error = "Input is missing or invalid. Check your CSV producer."
                for sensor in self.monitor.rules:
                    self.monitor.fault(sensor)
        else:
            phase = (time.monotonic() - self.started) % 90
            for index, (sensor, rule) in enumerate(self.monitor.rules.items()):
                center = (rule.minimum_c + rule.maximum_c) / 2
                value = center + math.sin(phase / 8 + index) * 0.6
                if 16 <= phase <= 34 and index == 0:
                    value = rule.maximum_c + 3 + math.sin(phase / 5)
                if 48 <= phase <= 66 and index == 1:
                    value = rule.minimum_c - 4 + math.sin(phase / 5)
                self.monitor.reading(sensor, value)
        self.monitor.tick()

    def deliver(self):
        if not self.notifier:
            return
        for event in self.monitor.undelivered():
            if self.stopped.is_set():
                break
            if event["id"] <= self.watermark:
                continue
            successful = self.notifier.send(event)
            self.monitor.delivered(event["id"], "sent" if successful else "failed")
            self.watermark = event["id"]

    def _loop(self, action, delay):
        while not self.stopped.is_set():
            action()
            self.stopped.wait(delay)

    def start(self):
        for action, delay in [(self.poll, 1), (self.deliver, 2)]:
            thread = threading.Thread(target=self._loop, args=(action, delay), daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stopped.set()
        for thread in self.threads:
            thread.join(timeout=10)

    def snapshot(self):
        return {**self.monitor.snapshot(), "mode": self.mode, "input_error": self.input_error, "webhook_enabled": bool(self.notifier), "uptime_seconds": round(time.monotonic()-self.started), "demo_cycle_seconds": 90}


def make_server(app, port=8766):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_):
            pass

        def reply(self, code, body=b"", content_type="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            actual_port = self.server.server_address[1]
            valid_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0] not in valid_hosts:
                self.reply(403, b"Unrecognized host")
                return False
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (origins and origins[0] not in {f"http://{host}" for host in valid_hosts}):
                self.reply(403, b"Unrecognized origin")
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.reply(403, b"Cross-site requests are not allowed")
                return False
            return True

        def do_GET(self):
            self.connection.settimeout(3)
            if not self.allowed():
                return
            if len(self.path) > 512:
                self.reply(414)
                return
            if self.path == "/api/state":
                self.reply(200, json.dumps(app.snapshot(), allow_nan=False).encode(), "application/json; charset=utf-8")
            elif self.path == "/health":
                self.reply(200, b'{"status":"ok"}', "application/json")
            elif self.path in ASSETS:
                filename, mime = ASSETS[self.path]
                self.reply(200, (STATIC / filename).read_bytes(), mime)
            else:
                self.reply(404, b"Not found")

        def do_POST(self):
            if self.allowed():
                self.reply(405, b"Dashboard is read-only")

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST
        do_OPTIONS = do_POST

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

    return LocalServer(("127.0.0.1", port), Handler)
