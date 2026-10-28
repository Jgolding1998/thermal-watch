from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname, timeout=4, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class Webhook:
    def __init__(self, url, resolver=socket.getaddrinfo, connection=PinnedHTTPS):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.fragment:
            raise ValueError("Webhook requires a public HTTPS endpoint on port 443 without credentials or a fragment")
        addresses = {item[4][0] for item in resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("Webhook DNS must resolve exclusively to public addresses")
        self.host = parsed.hostname
        self.address = sorted(addresses, key=lambda address: (ipaddress.ip_address(address).version, address))[0]
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.connection = connection

    def send(self, event):
        payload = json.dumps({"source": "thermal-watch", "event": event}, allow_nan=False).encode("utf-8")
        if len(payload) > 8192:
            return False
        connection = self.connection(self.host, self.address)
        try:
            connection.request("POST", self.path, payload, {"Content-Type": "application/json", "User-Agent": "thermal-watch/1.0", "Connection": "close"})
            response = connection.getresponse()
            response.read(1024)
            return 200 <= response.status < 300
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()
