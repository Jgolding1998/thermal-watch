import threading
import time
import unittest
import http.client
from pathlib import Path
from unittest.mock import patch

from thermal_watch.monitor import Monitor, Rule
from thermal_watch.notify import Webhook
from thermal_watch.server import Application, ApplicationClosed, make_server


def public_resolver(*args, **kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


class Response:
    status = 204

    def read(self, size):
        return b""

    def close(self):
        pass


class Connection:
    def __init__(self, *args):
        self.response = Response()
        self.closed = False

    def request(self, *args):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class DeliveryLifecycleTests(unittest.TestCase):
    def test_shutdown_rejects_unsupported_timeouts_before_stopping(self):
        monitor, _ = self.monitor()
        app = Application(monitor)
        for timeout in (10**400, 1e300, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout_type=type(timeout).__name__), self.assertRaises(ValueError):
                app.close(timeout=timeout)
            self.assertFalse(app.stopped.is_set())
            self.assertIsInstance(app.snapshot(), dict)
        app.close(timeout=0)

    def test_shutdown_clamps_the_join_wait_at_the_platform_limit(self):
        monitor, _ = self.monitor()
        app = Application(monitor)
        waits = []
        class Worker:
            def join(self, timeout):
                waits.append(timeout)
                if timeout > threading.TIMEOUT_MAX:
                    raise OverflowError("synthetic platform wait overflow")
        app.threads.append(Worker())
        clock = threading.TIMEOUT_MAX + 0.3
        with patch("thermal_watch.server.time.monotonic", return_value=clock):
            app.close(timeout=threading.TIMEOUT_MAX)
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0], threading.TIMEOUT_MAX)
        with self.assertRaises(ApplicationClosed):
            app.snapshot()

    def test_transport_initialization_failure_is_a_failed_attempt(self):
        def unavailable(*args):
            raise OSError("SYNTHETIC_PRIVATE_INITIALIZATION_DETAIL")

        hook = Webhook("https://example.test/", resolver=public_resolver, connection=unavailable)
        self.assertFalse(hook.send({"id": 1}))
        self.assertFalse(any(thread.name == "thermal-webhook-deadline" for thread in threading.enumerate()))

    def test_response_close_failure_preserves_accepted_result_and_closes_connection(self):
        connection = Connection()

        def failed_close():
            raise OSError("SYNTHETIC_PRIVATE_CLOSE_DETAIL")

        connection.response.close = failed_close
        hook = Webhook("https://example.test/", resolver=public_resolver, connection=lambda *args: connection)
        self.assertTrue(hook.send({"id": 1}))
        self.assertTrue(connection.closed)
        self.assertFalse(any(thread.name == "thermal-webhook-deadline" for thread in threading.enumerate()))

    def test_connection_close_failure_preserves_verified_accepted_result(self):
        connection = Connection()
        connection.close = lambda: (_ for _ in ()).throw(OSError("synthetic close failure"))
        hook = Webhook("https://example.test/", resolver=public_resolver, connection=lambda *args: connection)
        self.assertTrue(hook.send({"id": 1}))

    def monitor(self):
        now = [1000]
        monitor = Monitor([Rule("freezer", "Freezer", -24, -14, dwell_seconds=0)], clock=lambda: now[0])
        self.addCleanup(monitor.close)
        return monitor, now

    def test_notifier_exception_does_not_block_later_alarm_episodes(self):
        monitor, now = self.monitor()
        calls = []

        class Notifier:
            def send(self, event):
                calls.append(event["kind"])
                if len(calls) == 1:
                    raise RuntimeError("SYNTHETIC_PRIVATE_CALLBACK_DETAIL")
                return True

        app = Application(monitor, Path("synthetic.csv"), Notifier())
        monitor.reading("freezer", -10)
        app.deliver()
        now[0] += 1
        monitor.reading("freezer", -18)
        now[0] += 1
        monitor.reading("freezer", -10)
        app.deliver()
        app.deliver()
        self.assertEqual(calls, ["high", "recovered", "high"])
        events = list(reversed(monitor.snapshot()["events"]))
        self.assertEqual([event["delivery"] for event in events], ["failed", "sent", "sent"])
        self.assertNotIn("SYNTHETIC_PRIVATE", repr(events))

    def test_late_delivery_does_not_access_database_after_close_returns(self):
        monitor = Monitor([Rule("freezer", "Freezer", -24, -14, dwell_seconds=0)], clock=lambda: 1000)
        entered, release = threading.Event(), threading.Event()
        failures = []

        class Notifier:
            def send(self, event):
                entered.set()
                release.wait(2)
                return True

        app = Application(monitor, Path("synthetic.csv"), Notifier())
        monitor.reading("freezer", -10)

        def deliver():
            try:
                app.deliver()
            except Exception as error:
                failures.append(type(error).__name__)

        worker = threading.Thread(target=deliver)
        app.threads.append(worker)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            app.close(timeout=0.02)
            self.assertTrue(worker.is_alive())
            self.assertEqual(monitor.snapshot()["events"][0]["delivery"], "local")
            monitor.close()
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            app.deliver()
        finally:
            release.set()
            worker.join(2)

    def test_delivery_finished_within_shutdown_wait_is_recorded(self):
        monitor, _ = self.monitor()
        entered = threading.Event()

        class Notifier:
            def send(self, event):
                entered.set()
                time.sleep(0.02)
                return True

        app = Application(monitor, Path("synthetic.csv"), Notifier())
        monitor.reading("freezer", -10)
        worker = threading.Thread(target=app.deliver)
        app.threads.append(worker)
        worker.start()
        self.assertTrue(entered.wait(1))
        app.close(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(monitor.snapshot()["events"][0]["delivery"], "sent")

    def test_shutdown_wait_is_one_budget_for_all_workers(self):
        monitor, _ = self.monitor()
        release = threading.Event()
        app = Application(monitor)
        workers = [threading.Thread(target=release.wait, args=(2,)) for _ in range(3)]
        app.threads.extend(workers)
        for worker in workers:
            worker.start()
        try:
            started = time.monotonic()
            app.close(timeout=0.3)
            self.assertLess(time.monotonic() - started, 0.7)
        finally:
            release.set()
            for worker in workers:
                worker.join(1)

    def test_late_csv_parse_success_or_failure_does_not_touch_closed_database(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                monitor = Monitor([Rule("freezer", "Freezer", -24, -14)], clock=lambda: 1000)
                entered, release = threading.Event(), threading.Event()
                failures = []
                app = Application(monitor, Path("synthetic.csv"))

                def parse(*args):
                    entered.set()
                    release.wait(2)
                    if failed:
                        raise OSError("synthetic delayed read failure")
                    return {"freezer": {"sensor": "freezer", "temperature_c": "-18", "observed_at": "1000"}}, 1

                def poll():
                    try:
                        app.poll()
                    except Exception as error:
                        failures.append(type(error).__name__)

                worker = threading.Thread(target=poll)
                app.threads.append(worker)
                with patch("thermal_watch.server.parse_csv", parse):
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        app.close(timeout=0.02)
                        self.assertTrue(worker.is_alive())
                        monitor.close()
                        release.set()
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(failures, [])
                        app.poll()
                    finally:
                        release.set()
                        worker.join(2)

    def test_late_http_snapshot_returns_fixed_unavailable_response(self):
        monitor = Monitor([Rule("freezer", "Freezer", -24, -14)])
        app = Application(monitor)
        server = make_server(app, 0)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        worker.start()
        try:
            app.close(timeout=0)
            monitor.close()
            with self.assertRaises(ApplicationClosed):
                app.snapshot()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("GET", "/api/state")
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                self.assertEqual(response.read(), b"Dashboard has stopped")
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertNotIn("Python", response.getheader("Server"))
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(1)
