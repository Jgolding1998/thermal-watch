from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
from urllib.parse import urlsplit


IPV6_PUBLIC_UNICAST = ipaddress.ip_network("2000::/3")
IPV6_SPECIAL = tuple(ipaddress.ip_network(value) for value in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20"))


def _public_address(address):
    parsed = ipaddress.ip_address(address)
    effective = parsed
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.scope_id is not None:
            return False
        if parsed.ipv4_mapped is not None:
            effective = parsed.ipv4_mapped
        elif (parsed.is_site_local or parsed not in IPV6_PUBLIC_UNICAST
              or any(parsed in network for network in IPV6_SPECIAL)):
            return False
    return effective.is_global and not effective.is_multicast and not effective.is_reserved


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


def _enforce_deadline(connection, finished, seconds):
    if finished.wait(seconds):
        return
    while not finished.wait(0.05):
        sock = getattr(connection, "sock", None)
        if sock is not None:
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass


class Webhook:
    def __init__(self, url, resolver=socket.getaddrinfo, connection=PinnedHTTPS, deadline=10):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.fragment:
            raise ValueError("Webhook requires a public HTTPS endpoint on port 443 without credentials or a fragment")
        addresses = {item[4][0] for item in resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not _public_address(address) for address in addresses):
            raise ValueError("Webhook DNS must resolve exclusively to public addresses")
        self.host = parsed.hostname
        self.address = sorted(addresses, key=lambda address: (ipaddress.ip_address(address).version, address))[0]
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.connection = connection
        self.deadline = deadline

    def send(self, event):
        payload = json.dumps({"source": "thermal-watch", "event": event}, allow_nan=False).encode("utf-8")
        if len(payload) > 8192:
            return False
        connection = self.connection(self.host, self.address)
        finished = threading.Event()
        threading.Thread(target=_enforce_deadline, args=(connection, finished, self.deadline), daemon=True).start()
        try:
            connection.request("POST", self.path, payload, {"Content-Type": "application/json", "User-Agent": "thermal-watch/1.0", "Connection": "close"})
            response = connection.getresponse()
            response.read(1024)
            return 200 <= response.status < 300
        except (OSError, http.client.HTTPException):
            return False
        finally:
            finished.set()
            connection.close()
