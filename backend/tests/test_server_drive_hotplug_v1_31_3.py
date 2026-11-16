"""V1.31.3: drives attached to the forensic server after startup are listed live.

The containers reach the Windows workstation helper as http://host.docker.internal:9876. When the
helper had to fall back to listening on http://127.0.0.1:9876/ (no http://+:9876/ reservation),
Windows' HTTP server refused every request whose Host was not 127.0.0.1:9876, so the helper looked
offline and the drive list stayed frozen at the drives Docker mounted at startup.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _StrictHostHelper(BaseHTTPRequestHandler):
    """Behaves like HttpListener with the prefix http://127.0.0.1:<port>/ (http.sys host matching)."""

    drives = ["C", "D", "G"]
    required_host = ""

    def log_message(self, *args):  # quiet
        return

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.headers.get("Host") != self.required_host:
            self._send(400, b"<h2>Bad Request - Invalid Hostname</h2>", "text/html")
            return
        if self.path == "/health":
            self._send(200, json.dumps({"ok": True, "helper_version": "5.60"}).encode())
        elif self.path == "/drives":
            vols = [{"letter": d, "drive_type": "removable" if d == "G" else "fixed", "ready": True} for d in self.drives]
            self._send(200, json.dumps({"ok": True, "drives": self.drives, "volumes": vols}).encode())
        else:
            self._send(404, b"{}")


@pytest.fixture()
def strict_helper(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StrictHostHelper)
    port = server.server_address[1]
    _StrictHostHelper.required_host = f"127.0.0.1:{port}"
    _StrictHostHelper.drives = ["C", "D", "G"]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    real = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        # host.docker.internal is the Windows host; here that is this machine.
        return real("127.0.0.1" if host == "host.docker.internal" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    from app.services import hostdrive_agent

    monkeypatch.setattr(hostdrive_agent, "DEFAULT_HELPER_URLS", (f"http://host.docker.internal:{port}",))
    yield port
    server.shutdown()


def test_host_header_names_the_helper_by_its_loopback_address():
    from app.services.hostdrive_agent import docker_host_header

    assert docker_host_header("http://host.docker.internal:9876/drives") == "127.0.0.1:9876"
    assert docker_host_header("http://gateway.docker.internal:9876") == "127.0.0.1:9876"
    assert docker_host_header("http://192.168.65.254:9876") == "127.0.0.1:9876"
    assert docker_host_header("http://127.0.0.1:9876") is None
    assert docker_host_header("http://10.0.0.5:9876") is None


def test_helper_bound_to_loopback_is_reached_and_a_new_drive_is_listed_live(strict_helper, monkeypatch):
    from app.services import drive_mount_agent, hostdrive_agent

    # Docker mounted C: and D: at startup; G: was plugged in afterwards.
    monkeypatch.setattr(drive_mount_agent, "mounted_letters", lambda: ["c", "d"])
    status = hostdrive_agent.probe_helper()
    assert status["ok"] is True, status
    listing = hostdrive_agent.proxy_drives()
    assert listing["live"] is True and listing["helper_error"] is None
    assert listing["drives"] == ["C", "D", "G"]
    g = next(v for v in listing["volumes"] if v["letter"] == "G")
    assert g["mounted"] is False and g["access_mode"] == "windows_helper" and g["drive_type"] == "removable"

    # The drive is removed again: the next listing no longer has it.
    _StrictHostHelper.drives = ["C", "D"]
    assert hostdrive_agent.proxy_drives()["drives"] == ["C", "D"]


def test_without_the_loopback_host_header_the_same_helper_refuses(strict_helper):
    import urllib.error
    import urllib.request

    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"http://host.docker.internal:{strict_helper}/health", timeout=3)
    assert err.value.code == 400


def test_unreachable_helper_is_reported_with_the_reason(monkeypatch):
    from app.services import drive_mount_agent, hostdrive_agent

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    monkeypatch.setattr(hostdrive_agent, "DEFAULT_HELPER_URLS", (f"http://127.0.0.1:{closed}",))
    monkeypatch.setattr(drive_mount_agent, "inspect_host_letters", lambda: [{"letter": "c", "mounted": True}])
    monkeypatch.setattr(drive_mount_agent, "mounted_letters", lambda: ["c"])
    listing = hostdrive_agent.proxy_drives()
    assert listing["live"] is False
    assert listing["drives"] == ["C"]
    assert "not running" in listing["helper_error"]
    assert listing["helper_errors"]


def test_reasons_for_an_unreachable_helper():
    from app.services.hostdrive_agent import _explain_helper_errors

    assert "127.0.0.1" in _explain_helper_errors(["http://host.docker.internal:9876: HTTP Error 400: Bad Request"])
    assert "HOST_DRIVE_HELPER_TOKEN" in _explain_helper_errors(["x: HTTP Error 401: Unauthorized"])
    assert "Firewall" in _explain_helper_errors(["x: timed out"])
    assert "not running" in _explain_helper_errors(["x: <urlopen error [Errno 111] Connection refused>"])


def test_mobile_bridge_sends_the_same_host_header(monkeypatch):
    from app.services.mobile_acquire import host_bridge

    monkeypatch.delenv("HOST_DRIVE_HELPER_TOKEN", raising=False)
    assert host_bridge.helper_auth_headers("http://host.docker.internal:9876/health") == {"Host": "127.0.0.1:9876"}
    assert host_bridge.helper_auth_headers("http://127.0.0.1:9876/health") == {}
    assert host_bridge.helper_auth_headers() == {}
