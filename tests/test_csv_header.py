import csv
import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from thermal_watch.__main__ import main
from thermal_watch.inputs import append_reading, parse_csv
from thermal_watch.monitor import Monitor, Rule
from thermal_watch.server import Application


class HeaderFailureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "snapshot.csv"
        self.content = "x" * (csv.field_size_limit() + 1) + "\n"
        self.path.write_text(self.content, encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_parser_and_writer_report_oversized_headers_as_input_errors(self):
        for content in (self.content, "sensor,temperature_c," + self.content, '"' + self.content):
            with self.subTest(header=content[:25]):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    parse_csv(self.path, {"freezer"})
                with self.assertRaises(ValueError):
                    append_reading(self.path, "freezer", -18, 1000)
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_producer_cli_uses_its_fixed_error_for_an_oversized_header(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = main(["record", "--file", str(self.path), "--sensor", "freezer", "--value", "-18"])
        self.assertEqual(result, 2)
        self.assertEqual(error.getvalue(), "Cannot start: ValueError. Check your command and local configuration.\n")

    def test_polling_loop_survives_bad_header_and_recovers_on_replacement(self):
        monitor = Monitor([Rule("freezer", "Freezer", -24, -14)], clock=lambda: 1000)
        app = Application(monitor, self.path)
        statuses, errors = [], []

        def step():
            app.poll()
            statuses.append(monitor.snapshot()["sensors"][0]["status"])
            if len(statuses) == 1:
                self.path.write_text("sensor,temperature_c,observed_at\nfreezer,-18,1000\n", encoding="utf-8")
            else:
                app.stopped.set()

        worker = threading.Thread(target=app._loop, args=(step, .001), daemon=True)
        app.threads.append(worker)
        try:
            with patch("threading.excepthook", side_effect=lambda args: errors.append(type(args.exc_value).__name__)):
                worker.start()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(statuses, ["fault", "normal"])
            self.assertIsNone(app.input_error)
        finally:
            app.close(timeout=1)
            monitor.close()
