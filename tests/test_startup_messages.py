import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import thermal_watch
from thermal_watch import __main__ as cli
from thermal_watch.__main__ import SAFE_REASONS, main
from thermal_watch.inputs import MAX_BYTES, SnapshotTooLarge, parse_csv
from thermal_watch.monitor import Monitor, Rule
from thermal_watch.server import Application

PACKAGE = Path(thermal_watch.__file__).resolve().parent


def run(argv):
    error = io.StringIO()
    with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
        result = main(argv)
    return result, error.getvalue()


class StartupMessageTests(unittest.TestCase):
    def test_predefined_validation_reason_is_printed(self):
        self.assertEqual(run(["serve", "--port", "0"]), (2, "Cannot start: Port must be between 1 and 65535.\n"))
        self.assertEqual(run(["serve", "--webhook"])[1], "Cannot start: Webhooks are disabled in demo mode; select --csv live input.\n")

    def test_producer_commands_use_their_own_operation_label(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "readings.csv"
            existing.write_text("kept\n", encoding="utf-8")
            self.assertEqual(run(["sample", "--output", str(existing)]),
                             (2, "Cannot create sample: Sample output already exists; choose a new file.\n"))
            self.assertEqual(existing.read_text(encoding="utf-8"), "kept\n")
            missing = Path(directory) / "missing"
            self.assertEqual(run(["sensor", "--device", str(missing), "--file", str(existing), "--sensor", "freezer", "--interval", "0"]),
                             (2, "Cannot start sensor producer: Sensor interval must be between 1 and 3600 seconds.\n"))
            self.assertEqual(run(["record", "--file", str(existing), "--sensor", "freezer", "--value", "nan"]),
                             (2, "Cannot record reading: Value must be finite and between -100 and 350 Celsius.\n"))

    def test_arbitrary_exception_text_is_never_printed(self):
        private = "C:\\Users\\fixture-person\\secret-config.json"
        for error in (ValueError(private), OSError(private), TypeError(private), ValueError("")):
            with self.subTest(kind=type(error).__name__), patch.object(cli, "load_rules", side_effect=error):
                result, output = run(["serve"])
            self.assertEqual(result, 2)
            self.assertEqual(output, f"Cannot start: {type(error).__name__}. Check your command and local configuration.\n")
            self.assertNotIn("fixture-person", output)

    def test_every_safe_reason_is_a_fixed_message_raised_by_the_package(self):
        source = "".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py") if path.name != "__main__.py")
        source += (PACKAGE / "__main__.py").read_text(encoding="utf-8").split("SAFE_REASONS", 1)[1].split("})", 1)[1]
        for reason in SAFE_REASONS:
            with self.subTest(reason=reason[:40]):
                self.assertRegex(source, r"raise \w+\(" + re.escape('"' + reason + '"'))
                self.assertNotRegex(reason, r"[{}\\/]|\.\w{2,4}\b")


class OversizedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "snapshot.csv"
        rows = "".join(f"freezer,-18,{1000 + n}\n" for n in range(70_000))
        self.content = "sensor,temperature_c,observed_at\n" + rows
        self.assertGreater(len(self.content.encode("utf-8")), MAX_BYTES)
        self.path.write_text(self.content, encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_dashboard_names_the_size_limit_and_faults_sensors_without_changing_the_file(self):
        monitor = Monitor([Rule("freezer", "Freezer", -24, -14)], clock=lambda: 1000)
        app = Application(monitor, self.path)
        try:
            with self.assertRaises(SnapshotTooLarge):
                parse_csv(self.path, monitor.rules)
            app.poll()
            self.assertIn("1 MiB snapshot limit", app.input_error)
            self.assertIn("one current row per sensor", app.input_error)
            self.assertNotIn(str(self.path), app.input_error)
            self.assertEqual(monitor.snapshot()["sensors"][0]["status"], "fault")
            self.assertEqual(self.path.read_text(encoding="utf-8"), self.content)
        finally:
            app.close(timeout=1)
            monitor.close()

    def test_record_reports_the_size_limit_and_leaves_history_in_place(self):
        result, output = run(["record", "--file", str(self.path), "--sensor", "freezer", "--value", "-18"])
        self.assertEqual(result, 2)
        self.assertTrue(output.startswith("Cannot record reading: CSV snapshot is larger than the 1 MiB limit and was left unchanged"))
        self.assertNotIn(self.directory.name, output)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.content)


if __name__ == "__main__":
    unittest.main()
