import tempfile
import unittest
from pathlib import Path

from thermal_watch.monitor import Monitor, Rule
from thermal_watch.server import Application


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class FaultRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.rule = Rule("freezer", "Test freezer", -24, -14, dwell_seconds=0, stale_seconds=20)
        self.monitor = Monitor([self.rule], clock=self.clock)

    def tearDown(self):
        self.monitor.close()

    def kinds(self):
        return [row["kind"] for row in self.monitor.snapshot()["events"]]

    def test_brief_fault_recovery_is_recorded_once_without_waiting_for_staleness(self):
        self.monitor.reading("freezer", -18)
        self.monitor.fault("freezer")
        self.clock.now += 1
        self.monitor.reading("freezer", -18)
        self.assertEqual(self.kinds(), ["online", "fault"])
        self.clock.now += 1
        self.monitor.reading("freezer", -18)
        self.assertEqual(self.kinds(), ["online", "fault"])

    def test_online_does_not_clear_an_active_temperature_alarm(self):
        self.monitor.reading("freezer", -10)
        self.monitor.fault("freezer")
        self.clock.now += 1
        self.monitor.reading("freezer", -10)
        state = self.monitor.snapshot()["sensors"][0]
        self.assertEqual(state["alarm"], "high")
        self.assertEqual(state["status"], "high")
        self.assertEqual(self.kinds(), ["online", "fault", "high"])

    def test_stale_and_fault_recovery_share_one_online_event(self):
        self.monitor.reading("freezer", -18)
        self.monitor.fault("freezer")
        self.clock.now += 21
        self.monitor.tick()
        self.monitor.reading("freezer", -18)
        self.assertEqual(self.kinds().count("online"), 1)
        self.assertEqual(self.kinds().count("fault"), 1)
        self.assertEqual(self.kinds().count("stale"), 1)

    def test_old_and_duplicate_readings_do_not_claim_input_recovery(self):
        self.monitor.reading("freezer", -18, observed_at=900)
        self.monitor.fault("freezer")
        self.assertFalse(self.monitor.reading("freezer", -18, observed_at=900))
        self.monitor.reading("freezer", -18, observed_at=910)
        self.assertEqual(self.monitor.snapshot()["sensors"][0]["status"], "fault")
        self.assertNotIn("online", self.kinds())
        self.clock.now += 1
        self.monitor.reading("freezer", -18)
        self.assertEqual(self.kinds().count("online"), 1)

    def test_persisted_fault_recovery_is_a_new_deliverable_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.sqlite3"
            first = Monitor([self.rule], path, self.clock)
            first.reading("freezer", -18)
            first.fault("freezer")
            first.close()
            second = Monitor([self.rule], path, self.clock)
            try:
                class Sink:
                    def __init__(self):
                        self.events = []

                    def send(self, event):
                        self.events.append(event)
                        return True

                sink = Sink()
                app = Application(second, csv_path="configured.csv", notifier=sink)
                self.clock.now += 1
                second.reading("freezer", -18)
                app.deliver()
                app.deliver()
                self.clock.now += 1
                second.reading("freezer", -18)
                app.deliver()
                self.assertEqual([row["kind"] for row in sink.events], ["online"])
                app.close()
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main()
