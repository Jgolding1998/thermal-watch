from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .inputs import append_reading, ds18b20
from .monitor import Monitor, load_rules
from .notify import Webhook
from .server import Application, make_server

# Fixed validation reasons raised by this package. Only an exact match is printed, so exception
# text from libraries, paths and configured values never reach the terminal.
SAFE_REASONS = frozenset({
    "Sample output already exists; choose a new file",
    "Sensor interval must be between 1 and 3600 seconds",
    "Port must be between 1 and 65535",
    "Webhooks are disabled in demo mode; select --csv live input",
    "Set your own THERMAL_WEBHOOK_URL before enabling --webhook",
    "Webhook requires a public HTTPS endpoint on port 443 without credentials or a fragment",
    "Webhook DNS must resolve exclusively to public addresses within its deadline",
    "Configuration is too large",
    "Configuration contains duplicate keys",
    "Configuration must use finite numbers",
    "Configuration must be valid UTF-8 JSON",
    "Configuration must contain a sensors array",
    "Configure 1 to 16 sensors",
    "Each sensor must contain the required rule fields and no unknown fields",
    "Sensor IDs must be unique",
    "Sensor IDs must use 1 to 48 letters, numbers, underscores or hyphens",
    "Sensor labels must use 1 to 80 printable characters",
    "Thresholds and timing must be finite numbers",
    "Thresholds must be ordered and between -100 and 350 Celsius",
    "Hysteresis must be less than half the threshold range",
    "Invalid timing configuration",
    "CSV snapshot is larger than the 1 MiB limit and was left unchanged; keep one current row per sensor, as the record and sensor commands do",
    "CSV header must be sensor,temperature_c,observed_at",
    "CSV structure is invalid",
    "Another producer holds the CSV lock",
    "Value must be finite and between -100 and 350 Celsius",
    "Invalid sensor ID",
    "Existing snapshot contains a malformed row",
    "Snapshot contains too many sensors",
})


FAILURE_LABELS = {"record": "Cannot record reading", "sample": "Cannot create sample", "sensor": "Cannot start sensor producer"}


def failure_text(error, command="serve"):
    label = FAILURE_LABELS.get(command, "Cannot start")
    reason = str(error) if isinstance(error, ValueError) else ""
    if reason in SAFE_REASONS:
        return f"{label}: {reason}."
    return f"{label}: {type(error).__name__}. Check your command and local configuration."


def main(argv=None):
    parser = argparse.ArgumentParser(description="A local temperature dashboard. No account or API key required.")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Start the read-only local dashboard")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--config", type=Path)
    serve.add_argument("--csv", type=Path, help="Use a local CSV producer instead of simulated temperatures")
    serve.add_argument("--database", type=Path, default=Path("data/thermal.sqlite3"))
    serve.add_argument("--webhook", action="store_true", help="Send new live alerts to your own THERMAL_WEBHOOK_URL")
    record = sub.add_parser("record", help="Atomically update one sensor in a local CSV snapshot")
    record.add_argument("--file", type=Path, required=True)
    record.add_argument("--sensor", required=True)
    record.add_argument("--value", type=float, required=True)
    sample = sub.add_parser("sample", help="Create a fresh two-sensor sample CSV")
    sample.add_argument("--output", type=Path, default=Path("readings.csv"))
    sensor = sub.add_parser("sensor", help="Publish a Linux DS18B20 w1_slave sensor into a CSV")
    sensor.add_argument("--device", type=Path, required=True)
    sensor.add_argument("--file", type=Path, required=True)
    sensor.add_argument("--sensor", required=True)
    sensor.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    if not args.command:
        args = parser.parse_args(["serve"])
    try:
        if args.command == "record":
            append_reading(args.file, args.sensor, args.value)
            print("Reading stored in current snapshot")
            return 0
        if args.command == "sample":
            if args.output.exists():
                raise ValueError("Sample output already exists; choose a new file")
            append_reading(args.output, "freezer", -18)
            append_reading(args.output, "cooker", 76)
            print("Fresh sample created. Keep writing readings or the dashboard will report stale sensors.")
            return 0
        if args.command == "sensor":
            if not 1 <= args.interval <= 3600:
                raise ValueError("Sensor interval must be between 1 and 3600 seconds")
            print("Publishing local DS18B20 readings. Ctrl+C stops the producer.")
            while True:
                try:
                    append_reading(args.file, args.sensor, ds18b20(args.device))
                except (ValueError, OSError, UnicodeError):
                    print("Sensor input failed. No replacement reading was emitted.", file=sys.stderr)
                time.sleep(args.interval)
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        if args.webhook and not args.csv:
            raise ValueError("Webhooks are disabled in demo mode; select --csv live input")
        notifier = None
        if args.webhook:
            url = os.environ.get("THERMAL_WEBHOOK_URL")
            if not url:
                raise ValueError("Set your own THERMAL_WEBHOOK_URL before enabling --webhook")
            notifier = Webhook(url)
        rules = load_rules(args.config)
        database = args.database
        if not args.csv:
            database = ":memory:"
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
        monitor = Monitor(rules, database)
        app = Application(monitor, args.csv, notifier)
        server = make_server(app, args.port)
        app.start()
        print(f"Thermal Watch: http://127.0.0.1:{args.port}")
        print("LIVE local CSV input" if args.csv else "DEMO: all temperatures are simulated; no notifications leave this machine")
        print("Ctrl+C stops the dashboard.")
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            app.close()
            server.server_close()
            monitor.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, TypeError, OSError) as error:
        print(failure_text(error, args.command), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
