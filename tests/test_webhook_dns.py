import socket
import threading
import time
import unittest

from thermal_watch.notify import Webhook


class Response:
    status = 204
    def read(self, size):
        return b""
    def close(self):
        pass


class WebhookDNSTests(unittest.TestCase):
    def make_hook(self, resolver, **kwargs):
        connections = []
        class Connection:
            def __init__(self, host, address):
                connections.append((host, address))
            def request(self, *args):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        return Webhook("https://alerts.example.invalid/inbox", resolver=resolver, connection=Connection, **kwargs), connections

    def test_dns_rotation_is_checked_before_each_delivery(self):
        address = ["93.184.216.34"]
        def resolver(*args, **kwargs):
            return [(socket.AF_INET, 1, 6, "", (address[0], 443))]
        hook, connections = self.make_hook(resolver)
        self.assertTrue(hook.send({"id": 1}))
        address[0] = "8.8.8.8"
        self.assertTrue(hook.send({"id": 2}))
        self.assertEqual([item[1] for item in connections], ["93.184.216.34", "8.8.8.8"])
        address[0] = "127.0.0.1"
        self.assertFalse(hook.send({"id": 3}))
        self.assertEqual(len(connections), 2)

    def test_stuck_lookup_has_a_deadline_and_no_thread_accumulation(self):
        blocked, release = threading.Event(), threading.Event()
        count = [0]
        def resolver(*args, **kwargs):
            count[0] += 1
            if count[0] > 1:
                blocked.set()
                release.wait(2)
            return [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))]
        hook, connections = self.make_hook(resolver, deadline=0.05)
        try:
            started = time.monotonic()
            self.assertFalse(hook.send({"id": 1}))
            self.assertTrue(blocked.is_set())
            self.assertFalse(hook.send({"id": 2}))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(count[0], 2)
            self.assertEqual(connections, [])
        finally:
            release.set()
