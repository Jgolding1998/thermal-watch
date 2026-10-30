from __future__ import annotations

import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .inputs import SnapshotError, SnapshotTooLarge, apply_csv, parse_csv


STATIC = Path(__file__).parent / "static"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
ERRORS = {400: b"Invalid request", 404: b"Not found", 408: b"Request timed out", 414: b"Request too large", 431: b"Request headers too large", 501: b"Method not supported", 505: b"HTTP version not supported"}
HEADER_SECONDS = 5.0
MAX_CONNECTIONS = 16
BUSY_RESPONSE = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 11\r\n"
                 + b"".join(f"{name}: {value}\r\n".encode() for name, value in SECURITY_HEADERS.items())
                 + b"Connection: close\r\n\r\nServer busy")


class SocketDeadline:
    """Shuts a connection down at a fixed time unless cancelled. It never fires after cancel() returns."""

    def __init__(self, connection, seconds):
        self.connection, self.lock = connection, threading.Lock()
        self.cancelled = self.expired = False
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self):
        with self.lock:
            if self.cancelled:
                return
            self.expired = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def cancel(self):
        with self.lock:
            self.cancelled = True
        self.timer.cancel()
        return self.expired


class ApplicationClosed(RuntimeError):
    pass


class Application:
    def __init__(self, monitor, csv_path=None, notifier=None):
        self.monitor = monitor
        self.csv_path = csv_path
        self.notifier = notifier
        self.mode = "live" if csv_path else "demo"
        self.stopped = threading.Event()
        self.input_error = None
        self.started = time.monotonic()
        self.watermark = monitor.event_watermark()
        monitor.tick()
        self.threads = []
        self._lifecycle_lock = threading.Lock()
        self._closed = False

    def poll(self):
        with self._lifecycle_lock:
            if self._closed:
                return
        parsed, invalid = None, None
        if self.csv_path:
            try:
                parsed, _ = parse_csv(self.csv_path, self.monitor.rules)
            except SnapshotTooLarge:
                invalid = "The CSV is larger than the 1 MiB snapshot limit, so it was not read and sensors show a fault. Have the producer keep one current row per sensor, as the record and sensor commands do."
            except (OSError, ValueError, UnicodeError):
                invalid = "Input is missing or invalid. Check your CSV producer."
        with self._lifecycle_lock:
            if self._closed:
                return
            if self.csv_path:
                if invalid:
                    self.input_error = invalid
                    for sensor in self.monitor.rules:
                        self.monitor.fault(sensor)
                else:
                    try:
                        apply_csv(parsed, self.monitor)
                        self.input_error = None
                    except SnapshotError:
                        self.input_error = "One or more sensors have invalid input. Check the producer."
            else:
                phase = (time.monotonic() - self.started) % 90
                for index, (sensor, rule) in enumerate(self.monitor.rules.items()):
                    center = (rule.minimum_c + rule.maximum_c) / 2
                    value = center + math.sin(phase / 8 + index) * 0.6
                    if 16 <= phase <= 34 and index == 0:
                        value = rule.maximum_c + 3 + math.sin(phase / 5)
                    if 48 <= phase <= 66 and index == 1:
                        value = rule.minimum_c - 4 + math.sin(phase / 5)
                    value = max(-100, min(350, value))
                    self.monitor.reading(sensor, value)
            self.monitor.tick()

    def deliver(self):
        if not self.notifier:
            return
        with self._lifecycle_lock:
            if self._closed or self.stopped.is_set():
                return
            events = self.monitor.undelivered()
        for event in events:
            if self.stopped.is_set():
                break
            if event["id"] <= self.watermark:
                continue
            try:
                successful = self.notifier.send(event)
            except Exception:
                successful = False
            with self._lifecycle_lock:
                if self._closed:
                    return
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

    def close(self, timeout=10):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= threading.TIMEOUT_MAX or not math.isfinite(timeout):
            raise ValueError("Shutdown timeout must fit the supported nonnegative thread-wait range")
        self.stopped.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=min(threading.TIMEOUT_MAX, max(0, deadline - time.monotonic())))
        with self._lifecycle_lock:
            self._closed = True

    def snapshot(self):
        with self._lifecycle_lock:
            if self._closed:
                raise ApplicationClosed("Dashboard has stopped")
            return {**self.monitor.snapshot(), "mode": self.mode, "input_error": self.input_error, "webhook_enabled": bool(self.notifier), "uptime_seconds": round(time.monotonic()-self.started), "demo_cycle_seconds": 90}


def make_server(app, port=8766):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_):
            pass

        def version_string(self):
            return "Thermal Watch"

        def handle_one_request(self):
            self._header_deadline = SocketDeadline(self.connection, HEADER_SECONDS)
            try:
                super().handle_one_request()
            finally:
                self._header_deadline.cancel()

        def parse_request(self):
            try:
                accepted = super().parse_request()
            finally:
                expired = self._header_deadline.cancel()
            if expired:
                self.close_connection = True
                return False
            if accepted and self.request_version not in {"HTTP/1.0", "HTTP/1.1"}:
                self.send_error(400 if self.request_version == "HTTP/0.9" else 505)
                return False
            return accepted

        def send_error(self, code, message=None, explain=None):
            self.close_connection = True
            if getattr(self, "request_version", "HTTP/0.9") == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            self.reply(code, ERRORS.get(code, b"Request failed"))

        def reply(self, code, body=b"", content_type="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
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
                try:
                    state = app.snapshot()
                except ApplicationClosed:
                    self.reply(503, b"Dashboard has stopped")
                    return
                self.reply(200, json.dumps(state, allow_nan=False).encode(), "application/json; charset=utf-8")
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
        allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

        def __init__(self, *args):
            self.connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
            super().__init__(*args)

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

        def process_request(self, request, address):
            if not self.connections.acquire(blocking=False):
                try:
                    request.sendall(BUSY_RESPONSE)
                    request.shutdown(socket.SHUT_WR)
                    request.settimeout(0.05)
                    remaining = 16384
                    deadline = time.monotonic() + 0.1
                    while remaining > 0 and time.monotonic() < deadline:
                        data = request.recv(min(4096, remaining))
                        if not data:
                            break
                        remaining -= len(data)
                except OSError:
                    pass
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, address)
            except BaseException:
                self.connections.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self.connections.release()

        def handle_error(self, request, address):
            pass

    return LocalServer(("127.0.0.1", port), Handler)
