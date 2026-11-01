"""Run required browser checks against an owned in-memory synthetic dashboard."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from thermal_watch.monitor import Monitor, load_rules
from thermal_watch.server import Application, make_server


def main():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required for browser checks")
    monitor = Monitor(load_rules(None), ":memory:")
    app = Application(monitor)  # No CSV, hardware, notifier or external delivery.
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": .01}, daemon=True)
    captures = ROOT / "work" / "browser"
    captures.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update({"THERMAL_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                        "THERMAL_SCREENSHOT_DIR": str(captures)})
    thread.start()
    app.start()
    try:
        result = subprocess.run([node, str(ROOT / "tests" / "browser_smoke.cjs")],
                                cwd=ROOT, env=environment, timeout=120,
                                capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode == 0 and "Browser checks passed:" not in result.stdout:
            raise RuntimeError("Browser fixture did not report completed checks")
        return result.returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        app.close()
        monitor.close()


if __name__ == "__main__":
    raise SystemExit(main())
