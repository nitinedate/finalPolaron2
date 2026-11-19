"""V1.31.13: a phone appears only while it is plugged in, whichever helper list it comes through.

Every helper phone list the server passes on is trusted only while it is current (read within 2 minutes;
helper 5.64+ reads Windows at each request). There is no fallback any more to the phone list a helper reported
when it started.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services import hostdrive_agent as hda
from app.services.mobile_acquire import host_bridge

ROOT = Path(__file__).resolve().parents[2]


def _iso(delta_sec: float) -> str:
    # The helper writes .NET round-trip times: 7 fractional digits and Z. Computed when called: the full
    # suite runs for minutes, so a time taken at import would already be "old".
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_sec)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


IPHONE = {"adapter": "ios_lockdown", "device_id": "USB\\VID_05AC&PID_12A8\\00008110001A2B3C4D5E801E", "os_family": "ios"}


def test_staleness_rules():
    assert host_bridge.helper_list_staleness({"devices": [IPHONE], "scanned_utc": _iso(5)}) == ""
    old = host_bridge.helper_list_staleness({"devices": [IPHONE], "scanned_utc": _iso(13 * 60)})
    assert "13 min ago" in old and "not shown" in old and "-ForceRestart" in old
    # No time: a helper older than 5.61, whose list cannot be judged; only an empty list passes.
    assert "older than 5.61" in host_bridge.helper_list_staleness({"devices": [IPHONE]})
    assert host_bridge.helper_list_staleness({"devices": []}) == ""
    assert host_bridge.helper_list_staleness(None) == ""


def test_detect_drops_a_frozen_server_list(monkeypatch):
    lists = {"/acquisition/devices": {"devices": [IPHONE], "scanned_utc": _iso(15 * 60)}}
    monkeypatch.setattr(host_bridge, "_get_json", lambda path, timeout_sec=3.0: lists.get(path))
    devices, warnings = host_bridge.fetch_host_devices()
    assert devices == [] and "not updating" in warnings[0]
    lists["/acquisition/devices"]["scanned_utc"] = _iso(2)
    devices, warnings = host_bridge.fetch_host_devices()
    assert devices == [IPHONE]


def test_detect_fallback_trusts_only_a_live_mobile_list(monkeypatch):
    adb_row = {"id": "a1b2c3d4", "name": "Redmi Note 12", "connection": "adb", "os_hint": "android"}
    lists = {"/mobile-devices": {"mobile_devices": [adb_row]}}  # helper < 5.64: no read time
    monkeypatch.setattr(host_bridge, "_get_json", lambda path, timeout_sec=3.0: lists.get(path))
    devices, warnings = host_bridge.fetch_host_devices()
    assert devices == [] and warnings
    lists["/mobile-devices"]["scanned_utc"] = _iso(1)  # helper 5.64+: read live
    devices, _ = host_bridge.fetch_host_devices()
    assert devices[0]["adapter"] == "android_adb" and devices[0]["status"] == "adb"


def _office(monkeypatch, answers: dict, health: dict | None = None):
    monkeypatch.setattr(hda, "probe_helper", lambda: {"ok": True, "helper_url": "http://helper", **(health or {})})

    def fake(url: str, timeout: float = 8.0):
        for suffix, value in answers.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(url)

    monkeypatch.setattr(hda, "_http_json", fake)


def test_office_acquisition_list_is_age_checked(monkeypatch):
    _office(monkeypatch, {"/acquisition/devices": {"devices": [IPHONE], "scanned_utc": _iso(20 * 60)}})
    out = hda.proxy_acquisition_devices()
    assert out["devices"] == [] and out["stale"] is True and "not updating" in out["warnings"][-1]
    _office(monkeypatch, {"/acquisition/devices": {"devices": [IPHONE], "scanned_utc": _iso(1), "source": "live"}})
    out = hda.proxy_acquisition_devices()
    assert out["devices"][0]["attach_host"] == "office_server" and out["stale"] is False and out["list_source"] == "live"


def test_office_mobile_list_has_no_startup_fallback(monkeypatch):
    # The helper's health once carried the phones it saw at start; that list is never shown now.
    _office(monkeypatch, {"/mobile-devices": TimeoutError("timed out")}, health={"mobile_devices": [{"id": "ghost", "os_hint": "android"}]})
    out = hda.proxy_mobile_devices()
    assert out["mobile_devices"] == [] and "could not be read" in out["warnings"][0]


def test_office_mobile_list_of_an_older_helper_is_judged_by_its_device_list(monkeypatch):
    row = {"id": "x", "name": "Apple iPhone", "os_hint": "ios"}
    _office(monkeypatch, {
        "/mobile-devices": {"mobile_devices": [row]},
        "/acquisition/devices?cached=1": {"devices": [], "scanned_utc": _iso(30 * 60)},
    })
    out = hda.proxy_mobile_devices()
    assert out["mobile_devices"] == [] and "not updating" in out["warnings"][0]
    _office(monkeypatch, {
        "/mobile-devices": {"mobile_devices": [row]},
        "/acquisition/devices?cached=1": {"devices": [], "scanned_utc": _iso(3)},
    })
    assert [r["name"] for r in hda.proxy_mobile_devices()["mobile_devices"]] == ["Apple iPhone"]


def test_drive_answer_phones_only_from_a_live_helper():
    assert hda._helper_at_least({"helper_version": "5.65"}, "5.65")
    assert hda._helper_at_least({"helper_version": "5.100"}, "5.65")
    assert not hda._helper_at_least({"helper_version": "5.64"}, "5.65")
    assert not hda._helper_at_least({}, "5.65")
    src = (ROOT / "backend" / "app" / "services" / "hostdrive_agent.py").read_text(encoding="utf-8")
    assert 'if not _helper_at_least(health, "5.65"):\n                mobiles = []' in src
    helper = (ROOT / "scripts" / "host-drive-helper.ps1").read_text(encoding="utf-8")
    assert "if (liveOk) mobile = AetherisUsbLive.MobileRowsJson(livePhones);" in helper
