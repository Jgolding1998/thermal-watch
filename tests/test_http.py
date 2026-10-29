import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from thermal_watch.monitor import Monitor, Rule
from thermal_watch.server import Application, BUSY_RESPONSE, make_server


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor([Rule("freezer", "<script>label</script>", -24, -14)])
        self.monitor.reading("freezer", -18)
        self.app = Application(self.monitor)
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.monitor.close()

    def request(self, path, method="GET", headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        client.request(method, path, headers=headers or {})
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def test_state_and_headers(self):
        status, headers, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sensors"][0]["value_c"], -18)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_second_dashboard_cannot_share_the_active_port(self):
        other = None
        try:
            with self.assertRaises(OSError):
                other = make_server(self.app, self.port)
        finally:
            if other is not None:
                other.server_close()

    @unittest.skipUnless(hasattr(socket, "SO_EXCLUSIVEADDRUSE"), "Windows exclusive socket option")
    def test_windows_listener_excludes_reuse_address_contenders(self):
        self.assertEqual(self.server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
        with socket.socket() as contender:
            contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                contender.bind(self.server.server_address)

    def test_static_allowlist_and_traversal(self):
        for path in ["/", "/app.js", "/styles.css", "/health"]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 200)
        for path in ["/../config.json", "/%2e%2e/config.json", "/config.json", "/data/thermal.sqlite3", "/api/state?secret=value"]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_host_and_origin_rebinding_protection(self):
        for headers in [{"Host": "attacker.example.test"}, {"Origin": "https://attacker.example.test"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/state", headers=headers)[0], 403)

    def test_dashboard_has_no_mutating_api(self):
        for method in ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]:
            with self.subTest(method=method):
                self.assertEqual(self.request("/api/state", method)[0], 405)

    def raw(self, data):
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(data)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()

    def test_protocol_errors_are_controlled_and_versionless_requests_are_framed(self):
        host = b"Host: 127.0.0.1:" + str(self.port).encode() + b"\r\n"
        marker = b"fixture-request-marker"
        cases = [(b"UNSUPPORTED / HTTP/1.1\r\n" + host + b"\r\n", 501), (b"GET /?value=" + marker + b"\r\n\r\n", 400),
                 (b"GET / HTTP/9.9\r\n" + host + b"\r\n", 505), (b"GET / HTTP/1.2\r\n" + host + b"\r\n", 505)]
        for request, expected in cases:
            with self.subTest(request=request):
                status, headers, body = self.raw(request)
                self.assertEqual(status, expected)
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
                self.assertEqual(headers["server"], "Thermal Watch")
                self.assertNotIn(b"Python", body)
                self.assertNotIn(marker, body)
        self.assertEqual(self.request("/api/state")[0], 200)

    @patch("thermal_watch.server.HEADER_SECONDS", 0.3)
    def test_header_deadline_is_absolute_for_a_slow_drip_client(self):
        connection = socket.create_connection(self.server.server_address, timeout=10)
        stopped, received = threading.Event(), []
        def send():
            try:
                connection.sendall(b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:" + str(self.port).encode() + b"\r\n")
                while not stopped.wait(0.1):
                    connection.sendall(b"X-Drip: a\r\n")
            except OSError:
                pass
        started = time.monotonic()
        threading.Thread(target=send, daemon=True).start()
        try:
            while chunk := connection.recv(4096):
                received.append(chunk)
        except OSError:
            pass
        stopped.set()
        connection.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertNotIn(b"200 OK", b"".join(received))

    def test_connection_admission_limit_sends_a_controlled_busy_reply(self):
        with patch("thermal_watch.server.MAX_CONNECTIONS", 2):
            server = make_server(self.app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for _ in range(2):
            self.assertTrue(server.connections.acquire(blocking=False))
        try:
            for method, body in [("GET", None), ("POST", b"x" * 8192)]:
                for attempt in range(8):
                    with self.subTest(method=method, attempt=attempt):
                        client = http.client.HTTPConnection(*server.server_address, timeout=3)
                        try:
                            client.request(method, "/health", body=body)
                            response = client.getresponse()
                            self.assertEqual((response.status, response.read()), (503, b"Server busy"))
                            self.assertEqual(response.getheader("Cache-Control"), "no-store")
                        finally:
                            client.close()
        finally:
            for _ in range(2):
                server.connections.release()
        deadline, status = time.monotonic() + 5, None
        while status != 200 and time.monotonic() < deadline:
            client = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            client.request("GET", "/health")
            status = client.getresponse().status
            client.close()
        self.assertEqual(status, 200)

    def test_busy_response_drain_has_a_byte_limit_and_half_closes_first(self):
        with patch("thermal_watch.server.MAX_CONNECTIONS", 1):
            server = make_server(self.app, 0)
        self.addCleanup(server.server_close)
        self.assertTrue(server.connections.acquire(blocking=False))
        request = Mock()
        request.recv.return_value = b"x" * 4096
        with patch("thermal_watch.server.time.monotonic", return_value=0):
            server.process_request(request, ("127.0.0.1", 1))
        request.sendall.assert_called_once_with(BUSY_RESPONSE)
        self.assertEqual([call[0] for call in request.method_calls[:3]], ["sendall", "shutdown", "settimeout"])
        self.assertEqual(request.method_calls[1].args, (socket.SHUT_WR,))
        request.settimeout.assert_called_once_with(0.05)
        self.assertEqual(request.recv.call_count, 4)
        self.assertTrue(all(call.args[0] <= 4096 for call in request.recv.call_args_list))
        request.close.assert_called_once()

    def test_busy_response_drain_stops_at_the_deadline_or_socket_timeout(self):
        with patch("thermal_watch.server.MAX_CONNECTIONS", 1):
            server = make_server(self.app, 0)
        self.addCleanup(server.server_close)
        self.assertTrue(server.connections.acquire(blocking=False))
        for timeout in (False, True):
            with self.subTest(socket_timeout=timeout):
                request = Mock()
                if timeout:
                    request.recv.side_effect = socket.timeout()
                else:
                    request.recv.return_value = b"x"
                with patch("thermal_watch.server.time.monotonic", side_effect=[10.0, 10.01, 10.11]):
                    server.process_request(request, ("127.0.0.1", 1))
                self.assertEqual(request.recv.call_count, 1)
                request.close.assert_called_once()
