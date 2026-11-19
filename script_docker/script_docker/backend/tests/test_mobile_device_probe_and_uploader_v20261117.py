"""Windows USB model identification and agent upload validation contract tests."""
import importlib.util
import json
import pathlib
from unittest.mock import patch

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name[:-3], SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_connection_is_empty():
    probe = load_script("mobile_device_probe.py")
    with patch.object(probe, "execute", return_value="List of devices attached"):
        assert probe.android_rows("adb") == []
    with patch.object(probe, "execute", return_value=""):
        assert probe.ios_rows("idevice_id", "ideviceinfo") == []


def test_android_model_from_authorized_adb():
    probe = load_script("mobile_device_probe.py")
    def exec_mock(cmd, seconds=3):
        if "devices" in cmd:
            return "List of devices attached\nRZ1234 device product:a30s model:SM_A307FN\n"
        if cmd[-1] == "ro.product.model":
            return "SM-A307FN"
        if cmd[-1] == "ro.product.manufacturer":
            return "samsung"
        return ""
    with patch.object(probe, "execute", side_effect=exec_mock):
        devices = probe.android_rows("adb")
    assert len(devices) == 1
    assert devices[0]["os_hint"] == "android"
    assert "SM-A307FN" in devices[0]["name"]
    assert devices[0]["id"] == "RZ1234"


def test_unauthorized_adb_does_not_guess_model():
    probe = load_script("mobile_device_probe.py")
    with patch.object(probe, "execute", return_value="List of devices attached\nABC unauthorized\n"):
        devices = probe.android_rows("adb")
    assert len(devices) == 1
    assert devices[0]["live_acquire"] is False
    assert "authorization required" in devices[0]["name"]


def test_iphone_only_ios_row():
    probe = load_script("mobile_device_probe.py")
    with patch.object(probe, "execute", side_effect=["000081010012ABCD12345", "iPhone15,3"]):
        devices = probe.ios_rows("idevice_id", "ideviceinfo")
    assert len(devices) == 1
    assert devices[0]["os_hint"] == "ios"
    assert devices[0]["model"] == "iPhone15,3"


def test_gateway_tls_for_remote_and_local_http():
    uploader = load_script("mobile_server_transfer.py")
    assert uploader.validate_gateway("https://example.org/api/acquisition/transfer").startswith("https://")
    assert uploader.validate_gateway("http://127.0.0.1:3001/api/acquisition/transfer").startswith("http://")
    with pytest.raises(ValueError):
        uploader.validate_gateway("http://forensic.example/api/acquisition/transfer")
    with pytest.raises(ValueError):
        uploader.validate_gateway("https://user:pass@example.org/api/acquisition/transfer")


def test_uploader_requires_ticket_and_does_not_upload_any_image_to_browser(tmp_path):
    uploader = load_script("mobile_server_transfer.py")
    (tmp_path / "image.zip").write_bytes(b"evidence")
    with pytest.raises(ValueError, match="ticket"):
        uploader.upload_package({"adapter": "android_adb"}, tmp_path)


def test_exactly_one_full_zip_required(tmp_path):
    uploader = load_script("mobile_server_transfer.py")
    with pytest.raises(ValueError):
        uploader.full_package(tmp_path)
    (tmp_path / "image.zip").write_bytes(b"abc")
    assert uploader.full_package(tmp_path).name == "image.zip"
    (tmp_path / "second.zip").write_bytes(b"xyz")
    with pytest.raises(ValueError):
        uploader.full_package(tmp_path)


def test_agent_to_server_complete_evidence_transfer(tmp_path):
    """Real local HTTP PUT/POST streaming against the server-side chunk store."""
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from app.services.mobile_acquire.secure_transfer import MobileEvidenceTransfer

    uploader = load_script("mobile_server_transfer.py")
    store = MobileEvidenceTransfer(root=tmp_path / "server", secret="unit-test-secret")
    issued = store.ticket(tenant="qa-team", platform="android", case_id="CASE01", evidence_id="E01", examiner="qa")
    data = b"PK-evidence-stream\n" * 550_000  # >8 MiB, forces two chunks
    package_dir = tmp_path / "client"
    package_dir.mkdir()
    package = package_dir / "android_image.zip"
    package.write_bytes(data)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def answer(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def validate(self):
            assert self.headers["X-Tenant"] == "qa-team"
            assert self.headers["X-Aetheris-Service"] == "mobile-android"
            return store.verify_ticket(self.headers["Authorization"].split(" ", 1)[1], service_platform="android")

        def do_PUT(self):
            claims = self.validate()
            part = int(self.path.rsplit("/", 1)[1])
            blob = self.rfile.read(int(self.headers["Content-Length"]))
            resp = store.put_part(claims, part, blob, self.headers["X-Chunk-SHA256"])
            self.answer(200, resp)

        def do_POST(self):
            claims = self.validate()
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            resp = store.complete(claims, part_count=body["part_count"],
                                  byte_count=body["size_bytes"], sha256=body["sha256"])
            self.answer(200, resp)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/acquisition/transfer"
        payload = {"adapter": "android_adb", "server_transfer": {
            **issued, "base_url": url,
        }}
        result = uploader.upload_package(payload, package_dir)
        assert result["server_stored"] and result["verified"]
        assert result["client_package_removed"]
        assert not package.exists()
        path = store.resolve(result["evidence_ref"], tenant="qa-team", platform="android", case_id="CASE01")
        assert path.stat().st_size == len(data)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == result["sha256"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_frontend_does_not_list_local_image_paths():
    ui = (SCRIPTS.parent / "frontend" / "src" / "pages" / "forensic" / "MobileAcquisitionPage.tsx").read_text(encoding="utf-8")
    assert "Object.entries(exportPackagesOf(result))" not in ui
    assert "Portable image packages (.zip / .ufdx / .ufd / .pas)" not in ui
    assert "result.server_transfer?.server_stored" in ui


def test_reject_unrecognized_phone_platform_in_frontend():
    source = (SCRIPTS.parent / "frontend" / "src" / "lib" / "hostDriveHelper.ts").read_text(encoding="utf-8")
    assert 'return null' in source
    assert 'hostMobileToAcquisitionDevice' in source
