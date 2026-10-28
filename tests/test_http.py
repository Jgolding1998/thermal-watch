import http.client
import json
import threading
import unittest

from thermal_watch.monitor import Monitor, Rule
from thermal_watch.server import Application, make_server


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor([Rule("freezer", "<script>label</script>", -24, -14)])
        self.monitor.reading("freezer", -18)
        self.app = Application(self.monitor)
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.monitor.close()

    def request(self, path, method="GET", headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        client.request(method, path, headers=headers or {})
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def test_state_and_headers(self):
        status, headers, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sensors"][0]["value_c"], -18)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_static_allowlist_and_traversal(self):
        for path in ["/", "/app.js", "/styles.css", "/health"]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 200)
        for path in ["/../config.json", "/%2e%2e/config.json", "/config.json", "/data/thermal.sqlite3", "/api/state?secret=value"]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_host_and_origin_rebinding_protection(self):
        for headers in [{"Host": "attacker.example.test"}, {"Origin": "https://attacker.example.test"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/state", headers=headers)[0], 403)

    def test_dashboard_has_no_mutating_api(self):
        for method in ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]:
            with self.subTest(method=method):
                self.assertEqual(self.request("/api/state", method)[0], 405)
