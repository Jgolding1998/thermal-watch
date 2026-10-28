import csv
import io
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thermal_watch.__main__ import main
from thermal_watch.inputs import append_reading, ds18b20, read_csv
from thermal_watch.monitor import Monitor, Rule
from thermal_watch.notify import Webhook
from thermal_watch.server import Application


def produce_snapshot(path, sensor, count):
    for index in range(count):
        append_reading(Path(path), sensor, -18 + index / 1000, 1000 + index)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "readings.csv"
        self.monitor = Monitor([Rule("freezer", "Freezer", -24, -14)], clock=lambda: 1000)

    def tearDown(self):
        self.monitor.close()
        self.directory.cleanup()

    def test_csv_real_roundtrip_and_replay(self):
        append_reading(self.path, "freezer", -18, 1000)
        self.assertEqual(read_csv(self.path, self.monitor), 1)
        read_csv(self.path, self.monitor)
        self.assertEqual(self.monitor.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1)

    def test_bad_csv_is_a_fault(self):
        self.path.write_text("sensor,temperature_c,observed_at\nfreezer,nan,1000\n")
        with self.assertRaises(ValueError):
            read_csv(self.path, self.monitor)
        self.assertEqual(self.monitor.snapshot()["sensors"][0]["status"], "fault")

    def test_extra_columns_unknown_sensor_and_oversize(self):
        for content in ["sensor,temperature_c,observed_at\nfreezer,-18,1000,extra\n", "sensor,temperature_c,observed_at\nunknown,-18,1000\n", "wrong,header\n", "x" * 1_048_577]:
            self.path.write_text(content)
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                read_csv(self.path, self.monitor)

    def test_oversized_field_is_reported_without_killing_input_worker(self):
        self.path.write_text("sensor,temperature_c,observed_at\nfreezer," + "1" * 200000 + ",1000\n")
        app = Application(self.monitor, self.path)
        app.poll()
        self.assertIsNotNone(app.input_error)
        self.assertEqual(self.monitor.snapshot()["sensors"][0]["status"], "fault")

    def test_writer_compacts_old_journal_and_preserves_other_sensor(self):
        with self.path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sensor", "temperature_c", "observed_at"])
            for index in range(10001):
                writer.writerow(["freezer", -18, index])
            writer.writerow(["cooker", 76, 1000])
        append_reading(self.path, "freezer", -19, 1000)
        with self.path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["sensor"] for row in rows}, {"freezer", "cooker"})
        self.assertLess(self.path.stat().st_size, 200)

    def test_multiple_processes_preserve_each_sensor_snapshot(self):
        context = multiprocessing.get_context("spawn")
        workers = [context.Process(target=produce_snapshot, args=(str(self.path), sensor, 50)) for sensor in ["freezer", "cooker"]]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            self.assertEqual(worker.exitcode, 0)
        with self.path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["sensor"] for row in rows}, {"freezer", "cooker"})
        self.assertTrue(all(float(row["observed_at"]) == 1049 for row in rows))

    def test_legacy_snapshot_uses_final_physical_row_after_clock_rollback(self):
        self.path.write_text("sensor,temperature_c,observed_at\nfreezer,-18,5000\n")
        self.monitor.clock = lambda: 5000
        read_csv(self.path, self.monitor)
        self.monitor.clock = lambda: 1002
        self.path.write_text("sensor,temperature_c,observed_at\nfreezer,-18,5000\nfreezer,-19,1001\n")
        read_csv(self.path, self.monitor)
        self.assertEqual(self.monitor.snapshot()["sensors"][0]["status"], "normal")
        self.assertEqual(self.monitor.snapshot()["sensors"][0]["value_c"], -19)

    def test_writer_replaces_future_snapshot_after_clock_rollback(self):
        self.monitor.clock = lambda: 5000
        append_reading(self.path, "freezer", -18, 5000)
        read_csv(self.path, self.monitor)
        self.monitor.clock = lambda: 1002
        append_reading(self.path, "freezer", -19, 1001)
        read_csv(self.path, self.monitor)
        self.assertEqual(self.monitor.snapshot()["sensors"][0]["status"], "normal")

    def test_invalid_final_row_faults_only_affected_sensor(self):
        monitor = Monitor([Rule("freezer", "Freezer", -24, -14), Rule("cooker", "Cooker", 60, 95)], clock=lambda: 1000)
        self.path.write_text("sensor,temperature_c,observed_at\nfreezer,-18,1000\ncooker,nan,1000\n")
        app = Application(monitor, self.path)
        app.poll()
        sensors = {item["id"]: item for item in app.snapshot()["sensors"]}
        self.assertEqual(sensors["freezer"]["status"], "normal")
        self.assertEqual(sensors["cooker"]["status"], "fault")
        self.assertIsNotNone(app.input_error)
        monitor.close()

    def test_reading_long_bounded_legacy_journal_is_not_lifetime_limited(self):
        with self.path.open("w") as handle:
            handle.write("sensor,temperature_c,observed_at\n")
            for index in range(10001):
                handle.write(f"freezer,-18,{index / 10}\n")
        self.assertEqual(read_csv(self.path, self.monitor), 10001)
        self.assertEqual(self.monitor.snapshot()["sensors"][0]["status"], "normal")

    def test_ds18b20_crc_and_sentinels(self):
        self.path.write_text("00 00 00 : crc=00 YES\n00 00 00 t=-18500\n")
        self.assertEqual(ds18b20(self.path), -18.5)
        self.path.write_text("00 YES\n00 t=85000\n")
        self.assertEqual(ds18b20(self.path), 85.0)
        for value in ["00 NO\n00 t=-18000\n", "00 YES\n00 t=-127000\n", "00 YES\n00 t=126000\n", "00 YES\n00 t=nan\n"]:
            self.path.write_text(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                ds18b20(self.path)

    def test_missing_csv_faults_all_sensors(self):
        app = Application(self.monitor, self.path)
        app.poll()
        self.assertIsNotNone(app.snapshot()["input_error"])
        self.assertEqual(app.snapshot()["sensors"][0]["status"], "fault")

    def test_cli_requires_explicit_live_mode_for_webhook(self):
        with patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(main(["serve", "--webhook"]), 2)

    def test_sample_will_not_overwrite(self):
        self.path.write_text("keep")
        with patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(main(["sample", "--output", str(self.path)]), 2)
        self.assertEqual(self.path.read_text(), "keep")


class FakeResponse:
    status = 204

    def read(self, *_):
        return b""


class FakeConnection:
    response = FakeResponse()
    requests = []

    def __init__(self, host, address):
        self.host, self.address = host, address

    def request(self, method, path, body, headers):
        self.requests.append((method, path, json.loads(body), headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


def public_resolver(*_, **__):
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


class WebhookTests(unittest.TestCase):
    def test_valid_post_contract_and_dns_pinning(self):
        hook = Webhook("https://alerts.example.test/inbox", resolver=public_resolver, connection=FakeConnection)
        event = {"id": 7, "kind": "high", "message": "Freezer high"}
        self.assertTrue(hook.send(event))
        request = FakeConnection.requests[-1]
        self.assertEqual(request[0:2], ("POST", "/inbox"))
        self.assertEqual(request[2]["event"], event)
        self.assertEqual(hook.address, "93.184.216.34")

    def test_ipv4_is_preferred_when_public_dns_is_dual_stack(self):
        def dual_stack(*_, **__):
            return [(10, 1, 6, "", ("2001:4860:4860::8888", 443)), (2, 1, 6, "", ("93.184.216.34", 443))]
        hook = Webhook("https://alerts.example.test", resolver=dual_stack)
        self.assertEqual(hook.address, "93.184.216.34")

    def test_unsafe_urls_and_nonpublic_dns_rejected(self):
        userinfo_url = "https://user:pass" + chr(64) + "example.test"
        for url in ["http://example.test", userinfo_url, "https://example.test:8443", "https://example.test/#fragment", "file:///tmp/value"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Webhook(url, resolver=public_resolver)
        for address in ["127.0.0.1", "192.168.1.2", "169.254.169.254", "::1", "fd00::1"]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                Webhook("https://example.test", resolver=lambda *a, **k: [(2, 1, 6, "", (address, 443))])

    def test_failure_and_redirect_are_not_success(self):
        class Failing(FakeConnection):
            def request(self, *_):
                raise OSError("not exposed")
        hook = Webhook("https://example.test", resolver=public_resolver, connection=Failing)
        self.assertFalse(hook.send({"id": 1}))
        class Redirect(FakeConnection):
            def getresponse(self):
                response = FakeResponse()
                response.status = 302
                return response
        hook.connection = Redirect
        self.assertFalse(hook.send({"id": 1}))

    def test_old_events_not_sent_and_failures_are_recorded(self):
        monitor = Monitor([Rule("freezer", "Freezer", -24, -14)], clock=lambda: 1000)
        monitor.fault("freezer")
        class Fail:
            calls = []
            def send(self, event):
                self.calls.append(event)
                return False
        notifier = Fail()
        app = Application(monitor, Path("unused.csv"), notifier)
        app.deliver()
        self.assertEqual(notifier.calls, [])
        monitor.reading("freezer", -18)
        monitor.fault("freezer")
        app.deliver()
        app.deliver()
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(monitor.snapshot()["events"][0]["delivery"], "failed")
        monitor.close()
