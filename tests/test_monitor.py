import json
import contextlib
import io
import math
import tempfile
import unittest
from pathlib import Path

from thermal_watch.monitor import Monitor, Rule, load_rules
from thermal_watch.__main__ import main


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.rule = Rule("freezer", "Test freezer", -24, -14, 1, 10, 20, 60)
        self.monitor = Monitor([self.rule], clock=self.clock)

    def tearDown(self):
        self.monitor.close()

    def state(self):
        return self.monitor.snapshot()["sensors"][0]

    def kinds(self):
        return [event["kind"] for event in self.monitor.snapshot()["events"]]

    def read(self, value, advance=0):
        self.clock.advance(advance)
        self.monitor.reading("freezer", value)

    def test_waits_for_first_reading(self):
        self.assertEqual(self.state()["status"], "waiting")
        self.clock.advance(21)
        self.assertEqual(self.state()["status"], "stale")

    def test_threshold_boundaries_are_inclusive(self):
        self.read(-24)
        self.assertEqual(self.state()["status"], "normal")
        self.read(-14, 1)
        self.assertEqual(self.state()["status"], "normal")

    def test_high_requires_sustained_readings(self):
        self.read(-10)
        self.assertEqual(self.state()["status"], "pending")
        self.read(-10, 9.9)
        self.assertEqual(self.state()["status"], "pending")
        self.read(-10, .1)
        self.assertEqual(self.state()["status"], "high")
        self.assertEqual(self.kinds().count("high"), 1)

    def test_low_and_short_blip(self):
        self.read(-30)
        self.read(-18, 6)
        self.read(-30, 1)
        self.read(-30, 10)
        self.assertEqual(self.state()["status"], "low")
        self.assertEqual(self.kinds(), ["low"])

    def test_no_flapping_until_recovery_band(self):
        self.read(-10)
        self.read(-10, 10)
        for value in [-14, -14.5, -14.1, -14.9]:
            self.read(value, 1)
        self.assertEqual(self.state()["status"], "high")
        self.read(-15, 1)
        self.assertEqual(self.state()["status"], "normal")
        self.assertEqual(self.kinds().count("recovered"), 1)

    def test_opposite_excursion_requires_dwell_without_false_recovery(self):
        for first, opposite, original_kind, opposite_kind in [(-10, -30, "high", "low"), (-30, -10, "low", "high")]:
            with self.subTest(original=original_kind):
                clock = Clock()
                monitor = Monitor([self.rule], clock=clock)
                try:
                    monitor.reading("freezer", first)
                    clock.advance(10)
                    monitor.reading("freezer", first)
                    clock.advance(1)
                    monitor.reading("freezer", opposite)
                    state = monitor.snapshot()["sensors"][0]
                    self.assertEqual(state["alarm"], original_kind)
                    self.assertEqual(state["pending"], opposite_kind)
                    clock.advance(9.9)
                    monitor.reading("freezer", opposite)
                    self.assertEqual(monitor.snapshot()["sensors"][0]["alarm"], original_kind)
                    clock.advance(.1)
                    monitor.reading("freezer", opposite)
                    state = monitor.snapshot()["sensors"][0]
                    self.assertEqual(state["alarm"], opposite_kind)
                    self.assertIsNone(state["pending"])
                    self.assertEqual([e["kind"] for e in reversed(monitor.snapshot()["events"])], [original_kind, opposite_kind])
                finally:
                    monitor.close()

    def test_opposite_pending_resets_when_reading_returns_to_latched_alarm(self):
        self.read(-10)
        self.read(-10, 10)
        self.read(-30, 1)
        self.read(-10, 9)
        self.assertIsNone(self.state()["pending"])
        self.read(-30, 1)
        self.read(-30, 9)
        self.assertEqual(self.state()["alarm"], "high")
        self.read(-30, 1)
        self.assertEqual(self.state()["alarm"], "low")
        self.assertNotIn("recovered", self.kinds())

    def test_opposite_excursion_can_recover_only_after_returning_in_range(self):
        self.read(-10)
        self.read(-10, 10)
        self.read(-30, 1)
        self.read(-14.5, 1)
        self.assertEqual(self.state()["alarm"], "high")
        self.assertIsNone(self.state()["pending"])
        self.assertNotIn("recovered", self.kinds())
        self.read(-15, 1)
        self.assertIsNone(self.state()["alarm"])
        self.assertEqual(self.kinds(), ["recovered", "high"])

    def test_repeated_alarm_does_not_repeat_events(self):
        self.read(-10)
        for _ in range(80):
            self.read(-10, 2)
        self.assertEqual(self.kinds().count("high"), 1)

    def test_new_episode_always_alerts_inside_legacy_cooldown(self):
        self.read(-10)
        self.read(-10, 10)
        self.read(-18, 1)
        self.read(-10, 1)
        self.read(-10, 10)
        self.assertEqual(self.state()["status"], "high")
        self.assertEqual(self.kinds().count("high"), 2)
        self.assertEqual(self.kinds()[0], "high")
        self.read(-18, 60)
        self.read(-10, 1)
        self.read(-10, 10)
        self.assertEqual(self.kinds().count("high"), 3)

    def test_stale_is_distinct_and_recovers_once(self):
        self.read(-18)
        self.clock.advance(20)
        self.assertEqual(self.state()["status"], "normal")
        self.clock.advance(.1)
        self.assertEqual(self.state()["status"], "stale")
        self.clock.advance(30)
        self.assertEqual(self.state()["status"], "stale")
        self.read(-18, 1)
        self.assertEqual(self.state()["status"], "normal")
        self.assertEqual(self.kinds().count("stale"), 1)
        self.assertEqual(self.kinds().count("online"), 1)

    def test_missing_samples_do_not_satisfy_dwell(self):
        self.read(-10)
        self.read(-10, 21)
        self.assertEqual(self.state()["status"], "pending")
        self.assertNotIn("high", self.kinds())

    def test_old_samples_cannot_trigger_live_alarm(self):
        self.monitor.reading("freezer", -10, 900)
        self.assertEqual(self.state()["status"], "stale")
        self.assertNotIn("high", self.kinds())

    def test_clock_rollback_is_fault_and_accepts_new_current_reading(self):
        self.read(-18)
        self.clock.advance(-120)
        self.assertEqual(self.state()["status"], "fault")
        self.assertIn("backwards", self.state()["fault"])
        self.monitor.reading("freezer", -19)
        self.assertEqual(self.state()["status"], "normal")

    def test_clock_rollback_does_not_suppress_new_alarm(self):
        self.clock.now = 100000
        self.read(-10)
        self.read(-10, 10)
        self.read(-18, 1)
        self.clock.now = 1000
        self.read(-10)
        self.read(-10, 10)
        self.assertEqual(self.state()["status"], "high")
        self.assertEqual(self.kinds(), ["high", "recovered", "high"])

    def test_duplicate_or_out_of_order_is_ignored(self):
        self.read(-18)
        self.assertFalse(self.monitor.reading("freezer", -10, 1000))
        self.assertFalse(self.monitor.reading("freezer", -10, 999))
        self.assertEqual(self.state()["value_c"], -18)

    def test_nonfinite_sentinel_range_and_future_rejected(self):
        for value in [math.nan, math.inf, -math.inf, -127, 351, True, "-18"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.monitor.reading("freezer", value)
        for stamp in [math.nan, math.inf, -1, True, 1006]:
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.monitor.reading("freezer", -18, stamp)

    def test_fault_is_explicit_and_debounced(self):
        self.read(-18)
        self.monitor.fault("freezer")
        self.monitor.fault("freezer")
        self.assertEqual(self.state()["status"], "fault")
        self.assertEqual(self.kinds().count("fault"), 1)
        self.read(-18, 1)
        self.assertEqual(self.state()["status"], "normal")

    def test_unchanged_fault_does_not_rewrite_database(self):
        self.read(-18)
        self.monitor.fault("freezer")
        changes = self.monitor.db.total_changes
        for _ in range(100):
            self.monitor.fault("freezer")
        self.assertEqual(self.monitor.db.total_changes, changes)
        self.monitor.fault("freezer", "Producer reported a different fault")
        self.assertGreater(self.monitor.db.total_changes, changes)
        self.assertEqual(self.state()["fault"], "Producer reported a different fault")
        self.assertEqual(self.kinds().count("fault"), 1)

    def test_readings_are_bounded(self):
        for _ in range(300):
            self.read(-18, 1)
        self.assertEqual(self.monitor.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 240)
        self.assertEqual(len(self.state()["history"]), 90)

    def test_restart_preserves_alarm_without_resending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            one = Monitor([self.rule], path, self.clock)
            one.reading("freezer", -10)
            self.clock.advance(10)
            one.reading("freezer", -10)
            one.close()
            two = Monitor([self.rule], path, self.clock)
            self.clock.advance(1)
            two.reading("freezer", -10)
            self.assertEqual(two.snapshot()["sensors"][0]["status"], "high")
            self.assertEqual([e["kind"] for e in two.snapshot()["events"]].count("high"), 1)
            two.close()

    def test_restart_resets_pending_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            one = Monitor([self.rule], path, self.clock)
            one.reading("freezer", -10)
            one.close()
            self.clock.advance(12)
            two = Monitor([self.rule], path, self.clock)
            two.reading("freezer", -10)
            self.assertEqual(two.snapshot()["sensors"][0]["status"], "pending")
            two.close()

    def test_changed_rule_resets_persisted_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            one = Monitor([self.rule], path, self.clock)
            one.reading("freezer", -10)
            one.close()
            changed = Rule("freezer", "Test freezer", -24, -8, 1, 10, 20, 60)
            two = Monitor([changed], path, self.clock)
            self.assertEqual(two.snapshot()["sensors"][0]["status"], "waiting")
            two.close()


class ConfigurationTests(unittest.TestCase):
    def test_every_numeric_rule_field_rejects_huge_and_nonfinite_values(self):
        for field in ("minimum_c", "maximum_c", "hysteresis_c", "dwell_seconds", "stale_seconds", "cooldown_seconds"):
            for value in (10 ** 400, -(10 ** 400), math.inf, -math.inf, math.nan, True, "1"):
                data = dict(sensor="freezer", label="Freezer", minimum_c=-24, maximum_c=-14)
                data[field] = value
                with self.subTest(field=field, kind=type(value).__name__), self.assertRaises(ValueError):
                    Rule(**data)

    def test_huge_number_in_local_config_is_a_controlled_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"sensors":[dict(sensor="freezer", label="Private fixture label", minimum_c=10 ** 400, maximum_c=-14)]}))
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                result = main(["serve", "--config", str(path)])
            self.assertEqual(result, 2)
            self.assertEqual(output.getvalue(), "Cannot start: ValueError. Check your command and local configuration.\n")

    def test_invalid_rules(self):
        for kwargs in [{"minimum_c": -1, "maximum_c": -10}, {"hysteresis_c": 10}, {"dwell_seconds": -1}, {"stale_seconds": 0}, {"minimum_c": math.nan}, {"label": ""}, {"sensor": "../outside"}]:
            data = dict(sensor="freezer", label="Freezer", minimum_c=-24, maximum_c=-14)
            data.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Rule(**data)

    def test_config_schema_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            item = dict(sensor="freezer", label="Freezer", minimum_c=-24, maximum_c=-14)
            for document in [{"sensors": []}, {"sensors": [item, item]}, {"sensors": [item], "execute": "bad"}, {"sensors": "bad"}]:
                path.write_text(json.dumps(document))
                with self.subTest(document=document), self.assertRaises(ValueError):
                    load_rules(path)
