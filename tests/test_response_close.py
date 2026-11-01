"""Raw response-close regressions. No application, device or remote source runs."""
import http.client
import socket
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from thermal_watch import server as transport

MARKER = b"SYNTHETIC-NOT-FOR-RESPONSE"


class Inert:
    remote = None

    def __getattr__(self, name):
        raise AssertionError("Rejected request reached application: " + name)


def make_server():
    return transport.make_server(Inert(), 0)


class ResponseCloseTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server()
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def opening(self, method="POST", host=None, length=2048):
        return (f"{method} /synthetic-missing HTTP/1.1\r\nHost: {host or self.host}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n").encode()

    def response(self, client, expected):
        response = http.client.HTTPResponse(client)
        response.begin()
        body = response.read()
        self.assertEqual(response.status, expected)
        self.assertEqual(response.getheader("Content-Length"), str(len(body)))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertNotIn(MARKER, body)
        return body

    def wait_for_cleanup(self):
        slots = getattr(self.server, "connections")
        deadline = time.monotonic() + .7
        while True:
            acquired = 0
            try:
                while acquired < 16 and slots.acquire(blocking=False):
                    acquired += 1
                if acquired == 16:
                    return
            finally:
                for _ in range(acquired):
                    slots.release()
            if time.monotonic() >= deadline:
                self.fail("Response cleanup held a worker beyond its bound")
            time.sleep(.005)

    def delayed(self, method, host, expected):
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            client.sendall(self.opening(method, host))
            time.sleep(.08)
            client.sendall((MARKER + b"x" * 2048)[:2048])
            return self.response(client, expected)

    def test_delayed_post_preserves_exact_error_body_and_status(self):
        self.assertEqual(self.delayed("POST", self.host, 405), b'Dashboard is read-only')

    def test_delayed_foreign_host_still_refuses_before_application(self):
        self.delayed("GET", "foreign.invalid", 403)

    def test_delayed_get_retains_route_or_framing_refusal(self):
        self.delayed("GET", self.host, 404)

    def test_discarded_pipelined_request_never_produces_another_response(self):
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            body = b"{}\r\nGET / HTTP/1.1\r\nHost: " + self.host.encode() + b"\r\n\r\n" + MARKER
            client.sendall(self.opening(length=len(body)))
            time.sleep(.08)
            client.sendall(body)
            self.response(client, 405)
            self.assertEqual(client.recv(4096), b"")
        self.wait_for_cleanup()

    def test_peer_without_eof_cannot_hold_a_worker(self):
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            client.sendall(self.opening())
            self.response(client, 405)
            self.wait_for_cleanup()

    def test_slow_trickle_cannot_extend_cleanup_deadline(self):
        stopped = threading.Event()
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            client.sendall(self.opening())
            self.response(client, 405)
            def trickle():
                try:
                    while not stopped.wait(.015):
                        client.sendall(b"x")
                except OSError:
                    pass
            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            try:
                self.wait_for_cleanup()
            finally:
                stopped.set()
                sender.join(1)
            self.assertFalse(sender.is_alive())


class CleanupBoundsTests(unittest.TestCase):
    def test_discards_only_sixteen_kib_without_eof(self):
        request = MagicMock()
        request.recv.side_effect = lambda size: b"x" * size
        with patch.object(transport.time, "monotonic", return_value=1):
            transport._linger_discard(request)
        self.assertEqual([call.args[0] for call in request.recv.call_args_list], [4096] * 4)
        request.shutdown.assert_called_once_with(socket.SHUT_WR)

    def test_reads_use_remaining_absolute_budget(self):
        request = MagicMock()
        request.recv.return_value = b"x"
        with patch.object(transport.time, "monotonic", side_effect=[1, 1, 1.05, 1.101]):
            transport._linger_discard(request)
        self.assertEqual(request.recv.call_count, 2)
        self.assertAlmostEqual(request.settimeout.call_args_list[0].args[0], .1)
        self.assertAlmostEqual(request.settimeout.call_args_list[1].args[0], .05)

    def test_base_close_runs_even_if_cleanup_raises(self):
        request = MagicMock()
        server = make_server()
        try:
            with patch.object(transport, "_linger_discard", side_effect=RuntimeError("fixture")):
                with self.assertRaises(RuntimeError):
                    server.shutdown_request(request)
            request.close.assert_called_once_with()
        finally:
            server.server_close()

