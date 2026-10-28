from __future__ import annotations

import csv
import io
import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


FIELDS = ["sensor", "temperature_c", "observed_at"]
MAX_BYTES = 1_048_576
MAX_SENSORS = 64


class SnapshotError(ValueError):
    """Invalid final rows were already marked as per-sensor faults."""


def _rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        content = handle.read(MAX_BYTES + 1)
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Input exceeds the 1 MiB snapshot limit")
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames != FIELDS:
        raise ValueError("CSV header must be sensor,temperature_c,observed_at")
    try:
        yield from reader
    except csv.Error:
        raise ValueError("CSV structure is invalid") from None


def read_csv(path: Path, monitor):
    latest, count = {}, 0
    for row in _rows(path):
        count += 1
        sensor = row.get("sensor", "")
        if sensor not in monitor.rules:
            raise ValueError("CSV contains an unconfigured sensor")
        latest[sensor] = row
    invalid = False
    for sensor, row in latest.items():
        try:
            if set(row) != set(FIELDS) or None in row.values():
                raise ValueError("Malformed row")
            monitor.reading(sensor, float(row["temperature_c"]), float(row["observed_at"]))
        except (ValueError, TypeError):
            monitor.fault(sensor)
            invalid = True
    if invalid:
        raise SnapshotError("CSV contains an invalid final reading")
    return count


@contextmanager
def _writer_lock(path, timeout=2):
    handle = path.with_name(path.name + ".lock").open("a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"\0")
        handle.flush()
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("Another producer holds the CSV lock") from None
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _replace(path, rows):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", prefix=".thermal-", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            writer = csv.writer(handle)
            writer.writerow(FIELDS)
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + 2
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def append_reading(path: Path, sensor: str, value: float, observed_at: float | None = None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -100 <= value <= 350:
        raise ValueError("Value must be finite and between -100 and 350 Celsius")
    if not sensor or len(sensor) > 48 or not all(c.isalnum() or c in "_-" for c in sensor):
        raise ValueError("Invalid sensor ID")
    observed_at = time.time() if observed_at is None else observed_at
    if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at) or observed_at < 0:
        raise ValueError("Timestamp must be finite and nonnegative")
    with _writer_lock(path):
        latest = {}
        if path.exists() and path.stat().st_size:
            for row in _rows(path):
                key = row.get("sensor", "")
                if not key or len(key) > 48 or not all(c.isalnum() or c in "_-" for c in key) or set(row) != set(FIELDS) or None in row.values():
                    raise ValueError("Existing snapshot contains a malformed row")
                latest[key] = row
                if len(latest) > MAX_SENSORS:
                    raise ValueError("Snapshot contains too many sensors")
        latest[sensor] = dict(zip(FIELDS, [sensor, value, observed_at]))
        if len(latest) > MAX_SENSORS:
            raise ValueError("Snapshot contains too many sensors")
        _replace(path, ([row[field] for field in FIELDS] for row in latest.values()))


def ds18b20(path: Path) -> float:
    if path.stat().st_size > 4096:
        raise ValueError("Unexpected sensor response size")
    lines = path.read_text(encoding="ascii").strip().splitlines()
    if len(lines) != 2 or not lines[0].endswith("YES") or "t=" not in lines[1]:
        raise ValueError("Sensor CRC failed or payload is incomplete")
    value = int(lines[1].split("t=", 1)[1]) / 1000
    if not -55 <= value <= 125:
        raise ValueError("Sensor reading is outside its supported range")
    return value
