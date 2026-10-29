import threading
import unittest
from unittest.mock import patch

from thermal_watch.monitor import Monitor, Rule
from thermal_watch.server import Application


class DemoLimitTests(unittest.TestCase):
    def test_valid_edge_thresholds_keep_the_entire_demo_cycle_in_range(self):
        rules=[Rule("hot", "Hot edge", 340, 350), Rule("cold", "Cold edge", -100, -90),
               Rule("near_hot", "Near hot", 349.8, 350, 0), Rule("near_cold", "Near cold", -100, -99.8, 0)]
        monitor=Monitor(rules)
        app=Application(monitor)
        try:
            for phase in range(90):
                with self.subTest(phase=phase), patch("thermal_watch.server.time.monotonic", return_value=app.started+phase):
                    app.poll()
                    for state in monitor.snapshot()["sensors"]:
                        self.assertTrue(-100 <= state["value_c"] <= 350)
        finally:
            app.close()
            monitor.close()

    def test_default_demo_still_activates_and_recovers_both_alarm_examples(self):
        now=[1000]
        monitor=Monitor([Rule("freezer", "Freezer", -24, -14, 1, 8, 20), Rule("cooker", "Cooker", 60, 95, 2, 8, 20)], clock=lambda:now[0])
        app=Application(monitor)
        try:
            states={}
            for phase in (0, 20, 29, 40, 50, 59, 70):
                now[0]=1000+phase
                with patch("thermal_watch.server.time.monotonic", return_value=app.started+phase):
                    app.poll()
                states[phase]=[sensor["status"] for sensor in monitor.snapshot()["sensors"]]
            self.assertEqual(states[20][0], "pending")
            self.assertEqual(states[29][0], "high")
            self.assertEqual(states[40][0], "normal")
            self.assertEqual(states[50][1], "pending")
            self.assertEqual(states[59][1], "low")
            self.assertEqual(states[70][1], "normal")
        finally:
            app.close()
            monitor.close()

    def test_poll_worker_continues_after_both_edge_excursions(self):
        monitor=Monitor([Rule("hot", "Hot edge", 340, 350), Rule("cold", "Cold edge", -100, -90)])
        app=Application(monitor)
        phases=iter((20, 50, 70))
        seen, errors=[], []
        def step():
            phase=next(phases)
            with patch("thermal_watch.server.time.monotonic", return_value=app.started+phase):
                app.poll()
            seen.append(phase)
            if phase == 70:
                app.stopped.set()
        worker=threading.Thread(target=app._loop, args=(step, .001), daemon=True)
        app.threads.append(worker)
        try:
            with patch("threading.excepthook", side_effect=lambda args: errors.append(type(args.exc_value).__name__)):
                worker.start()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(seen, [20, 50, 70])
        finally:
            app.close()
            monitor.close()
