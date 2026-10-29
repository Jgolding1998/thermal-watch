from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Rule:
    sensor: str
    label: str
    minimum_c: float
    maximum_c: float
    hysteresis_c: float = 1.0
    dwell_seconds: float = 10.0
    stale_seconds: float = 30.0
    cooldown_seconds: float = 60.0

    def __post_init__(self):
        if not isinstance(self.sensor, str) or not self.sensor or len(self.sensor) > 48 or not all(c.isalnum() or c in "_-" for c in self.sensor):
            raise ValueError("Sensor IDs must use 1 to 48 letters, numbers, underscores or hyphens")
        if not isinstance(self.label, str) or not 1 <= len(self.label) <= 80:
            raise ValueError("Sensor labels must use 1 to 80 characters")
        numbers = (self.minimum_c, self.maximum_c, self.hysteresis_c, self.dwell_seconds, self.stale_seconds, self.cooldown_seconds)
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in numbers):
            raise ValueError("Thresholds and timing must be finite numbers")
        if not -100 <= self.minimum_c < self.maximum_c <= 350:
            raise ValueError("Thresholds must be ordered and between -100 and 350 Celsius")
        if not 0 <= self.hysteresis_c < (self.maximum_c - self.minimum_c) / 2:
            raise ValueError("Hysteresis must be less than half the threshold range")
        if not 0 <= self.dwell_seconds <= 3600 or not 1 <= self.stale_seconds <= 86400 or not 0 <= self.cooldown_seconds <= 86400:
            raise ValueError("Invalid timing configuration")


def load_rules(path: Path | None) -> list[Rule]:
    if path is None:
        return [Rule("freezer", "Garage freezer", -24, -14, 1, 8, 20, 60), Rule("cooker", "Countertop cooker", 60, 95, 2, 8, 20, 60)]
    if path.stat().st_size > 32768:
        raise ValueError("Configuration is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"sensors"} or not isinstance(data["sensors"], list):
        raise ValueError("Configuration must contain a sensors array")
    if not 1 <= len(data["sensors"]) <= 16:
        raise ValueError("Configure 1 to 16 sensors")
    rules = [Rule(**entry) for entry in data["sensors"]]
    if len({rule.sensor for rule in rules}) != len(rules):
        raise ValueError("Sensor IDs must be unique")
    return rules


class Monitor:
    def __init__(self, rules: list[Rule], database: str | Path = ":memory:", clock: Callable[[], float] = time.time):
        self.clock = clock
        self.started = clock()
        self.rules = {rule.sensor: rule for rule in rules}
        if not rules or len(self.rules) != len(rules):
            raise ValueError("Provide unique sensor rules")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(database), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state(sensor TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY, sensor TEXT NOT NULL, value REAL NOT NULL, observed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, sensor TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL, message TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'local');
            CREATE INDEX IF NOT EXISTS reading_sensor ON readings(sensor, id);
        """)
        self.states = {}
        for key, rule in self.rules.items():
            row = self.db.execute("SELECT payload FROM state WHERE sensor=?", (key,)).fetchone()
            saved = json.loads(row["payload"]) if row else {}
            same_rule = saved.get("rule") == asdict(rule)
            self.states[key] = saved if same_rule else self._empty(rule)
            self.states[key]["pending"] = None
            self.states[key]["pending_since"] = None
        self.db.commit()

    def _empty(self, rule):
        return {"rule": asdict(rule), "value_c": None, "observed_at": None, "alarm": None, "pending": None, "pending_since": None, "stale": False, "fault": None, "last_alert": {}}

    def _save(self, sensor):
        self.db.execute("INSERT INTO state(sensor,payload) VALUES(?,?) ON CONFLICT(sensor) DO UPDATE SET payload=excluded.payload", (sensor, json.dumps(self.states[sensor])))

    def _event(self, sensor, kind, message):
        now = self.clock()
        self.db.execute("INSERT INTO events(sensor,kind,created,message) VALUES(?,?,?,?)", (sensor, kind, now, message))
        self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")

    def _activate(self, sensor, kind):
        state, rule = self.states[sensor], self.rules[sensor]
        self._event(sensor, kind, f"{rule.label}: temperature is {kind}")
        state["alarm"] = kind
        state["pending"] = None
        state["pending_since"] = None

    def reading(self, sensor: str, value: float, observed_at: float | None = None) -> bool:
        with self.lock:
            if sensor not in self.rules:
                raise ValueError("Unknown sensor")
            now = self.clock()
            observed_at = now if observed_at is None else observed_at
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -100 <= value <= 350:
                raise ValueError("Reading must be a finite Celsius value between -100 and 350")
            if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at) or observed_at < 0 or observed_at > now + 5:
                raise ValueError("Reading timestamp is invalid or too far in the future")
            state, rule = self.states[sensor], self.rules[sensor]
            clock_rolled_back = state["observed_at"] is not None and state["observed_at"] > now + 5
            if state["observed_at"] is not None and observed_at <= state["observed_at"] and not clock_rolled_back:
                return False
            previous_observed = state["observed_at"]
            if clock_rolled_back or (previous_observed is not None and observed_at - previous_observed > rule.stale_seconds):
                state["pending"] = None
                state["pending_since"] = None
            state["value_c"] = value
            state["observed_at"] = observed_at
            state["fault"] = None
            fresh = now - observed_at <= rule.stale_seconds
            if state["stale"] and fresh:
                state["stale"] = False
                self._event(sensor, "online", f"{rule.label}: fresh readings resumed")
            self.db.execute("INSERT INTO readings(sensor,value,observed) VALUES(?,?,?)", (sensor, value, observed_at))
            self.db.execute("DELETE FROM readings WHERE sensor=? AND id NOT IN (SELECT id FROM readings WHERE sensor=? ORDER BY id DESC LIMIT 240)", (sensor, sensor))
            if fresh:
                self._evaluate(sensor, value, observed_at)
            self._save(sensor)
            self.db.commit()
            return True

    def _evaluate(self, sensor, value, observed_at):
        state, rule = self.states[sensor], self.rules[sensor]
        old_alarm = state["alarm"]
        direction = "low" if value < rule.minimum_c else "high" if value > rule.maximum_c else None
        cleared = direction is None and ((old_alarm == "low" and value >= rule.minimum_c + rule.hysteresis_c) or (old_alarm == "high" and value <= rule.maximum_c - rule.hysteresis_c))
        if cleared:
            self._event(sensor, "recovered", f"{rule.label}: temperature returned inside the recovery band")
            state["alarm"] = None
        if state["alarm"] and direction in (None, state["alarm"]):
            state["pending"] = None
            state["pending_since"] = None
            return
        if direction is None:
            state["pending"] = None
            state["pending_since"] = None
        elif state["pending"] != direction:
            state["pending"] = direction
            state["pending_since"] = observed_at
            if rule.dwell_seconds == 0:
                self._activate(sensor, direction)
        elif observed_at - state["pending_since"] >= rule.dwell_seconds:
            self._activate(sensor, direction)

    def fault(self, sensor, message="Invalid sensor input"):
        with self.lock:
            if sensor not in self.states:
                return
            state = self.states[sensor]
            message = message[:120]
            if state["fault"] == message and state["pending"] is None and state["pending_since"] is None:
                return
            if state["fault"] is None:
                self._event(sensor, "fault", f"{self.rules[sensor].label}: invalid or unreadable sensor input")
            state["fault"] = message
            state["pending"] = None
            state["pending_since"] = None
            self._save(sensor)
            self.db.commit()

    def tick(self):
        with self.lock:
            now = self.clock()
            for sensor, state in self.states.items():
                rule = self.rules[sensor]
                reference = state["observed_at"] if state["observed_at"] is not None else self.started
                if reference > now + 5:
                    self.fault(sensor, "System clock moved backwards; waiting for a current reading")
                    continue
                if now - reference > rule.stale_seconds and not state["stale"]:
                    state["stale"] = True
                    state["pending"] = None
                    state["pending_since"] = None
                    self._event(sensor, "stale", f"{rule.label}: no fresh readings received")
                    self._save(sensor)
            self.db.commit()

    def snapshot(self):
        self.tick()
        with self.lock:
            sensors = []
            now = self.clock()
            for sensor, state in self.states.items():
                rule = self.rules[sensor]
                status = "fault" if state["fault"] else "stale" if state["stale"] else state["alarm"] or ("pending" if state["pending"] else "waiting" if state["value_c"] is None else "normal")
                history = self.db.execute("SELECT value, observed FROM readings WHERE sensor=? ORDER BY id DESC LIMIT 90", (sensor,)).fetchall()
                sensors.append({"id": sensor, "label": rule.label, "value_c": state["value_c"], "minimum_c": rule.minimum_c, "maximum_c": rule.maximum_c, "status": status, "pending": state["pending"], "alarm": state["alarm"], "age_seconds": None if state["observed_at"] is None else max(0, round(now-state["observed_at"], 1)), "fault": state["fault"], "history": [{"value_c": r["value"], "at": r["observed"]} for r in reversed(history)]})
            events = [dict(row) for row in self.db.execute("SELECT id,sensor,kind,created,message,delivery FROM events ORDER BY id DESC LIMIT 60")]
            return {"now": now, "sensors": sensors, "events": events}

    def event_watermark(self):
        with self.lock:
            return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    def undelivered(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT id,sensor,kind,created,message FROM events WHERE delivery='local' ORDER BY id")]

    def delivered(self, event_id, status):
        if status not in {"sent", "failed"}:
            raise ValueError("Unknown delivery status")
        with self.lock:
            self.db.execute("UPDATE events SET delivery=? WHERE id=?", (status, event_id))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()
