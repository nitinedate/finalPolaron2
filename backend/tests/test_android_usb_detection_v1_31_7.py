"""V1.31.7: an Android phone on the client computer is listed even when Windows does not show it as a
portable device: by its File transfer (MTP) interface, or - when it is on charging only or has no Windows
driver for its USB mode - as a phone that needs something done on it, with what to do."""
from __future__ import annotations

from pathlib import Path

from app.services.mobile_acquire import host_bridge
from app.services.mobile_platform_agents import detect_mobile_platform

ROOT = Path(__file__).resolve().parents[2]
HELPER = (ROOT / "scripts" / "host-drive-helper.ps1").read_text(encoding="utf-8")

CHARGING_ONLY = {
    "id": "a1b2c3d4",
    "name": "Android phone",
    "device_class": "USB",
    "connection": "usb",
    "os_hint": "android",
    "instance_id": "USB\\VID_2717&PID_FF48\\a1b2c3d4",
    "live_acquire": False,
    "state": "charging_only",
    "capability_label": "NEEDS_ACTION",
    "guidance": "Connected for charging only. Unlock the phone ... choose File transfer ..., then Rescan.",
}


def _fresh(payload):
    # V1.31.13: helper lists carry the time they were read; these lists were just read.
    if isinstance(payload, dict) and "scanned_utc" not in payload:
        from datetime import datetime, timezone
        payload = {**payload, "scanned_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    return payload


def _fake_get_json(acq, mobile):
    acq, mobile = _fresh(acq), _fresh(mobile)

    def fake(path, timeout_sec=3.0):
        if path == "/acquisition/devices":
            return acq
        if path == "/mobile-devices":
            return mobile
        return None
    return fake


def test_mobile_list_fallback_keeps_a_needs_action_phone_out_of_mtp(monkeypatch):
    monkeypatch.setattr(host_bridge, "_get_json", _fake_get_json(None, {"mobile_devices": [
        CHARGING_ONLY,
        {"id": "wpd1", "name": "Galaxy S23", "connection": "mtp", "os_hint": "android", "instance_id": "x"},
    ]}))
    devices, warnings = host_bridge.fetch_host_devices()
    by_id = {d["device_id"]: d for d in devices}
    assert warnings == []
    usb = by_id["a1b2c3d4"]
    assert usb["adapter"] == "android_usb"
    assert usb["status"] == "charging_only" and usb["state"] == "charging_only"
    assert "File transfer" in usb["guidance"]
    assert by_id["wpd1"]["adapter"] == "android_mtp" and "guidance" not in by_id["wpd1"]


def test_helper_list_is_passed_through_with_guidance(monkeypatch):
    row = {"adapter": "android_usb", "device_id": "a1b2c3d4", "status": "driver_missing", "state": "driver_missing",
           "os_family": "android", "guidance": "Windows has no working driver ..."}
    monkeypatch.setattr(host_bridge, "_get_json", _fake_get_json({"devices": [row], "warnings": []}, None))
    devices, _ = host_bridge.fetch_host_devices()
    assert devices == [row]


def test_needs_action_adapter_is_android():
    assert detect_mobile_platform({"adapter": "android_usb", "device_id": "x"}) == "android"
    assert detect_mobile_platform("android_usb") == "android"


def test_malware_live_devices_marks_needs_action_phone_not_ready(monkeypatch):
    from app.malware import router as malware_router

    monkeypatch.setattr(host_bridge, "helper_reachable", lambda: True)
    monkeypatch.setattr(host_bridge, "helper_health", lambda: {"malware_triage": True, "helper_version": "5.62"})
    monkeypatch.setattr(host_bridge, "fetch_host_devices", lambda: ([{
        "adapter": "android_usb", "device_id": "a1b2c3d4", "os_family": "android", "status": "charging_only",
        "state": "charging_only", "guidance": "Choose File transfer on the phone, then Rescan.",
    }], []))
    out = malware_router.live_devices(None)
    assert out["devices"][0]["ready"] is False
    assert out["devices"][0]["hint"] == "Choose File transfer on the phone, then Rescan."
    assert out["devices"][0]["platform"] == "android"


def test_helper_recognises_android_by_its_usb_interfaces():
    assert 'HelperVersion = "5.66"' in HELPER  # 5.62 Android USB states; 5.63 V1.31.8; 5.64 V1.31.9; 5.65 V1.31.13; 5.66 V1.31.14
    assert HELPER.count("function Get-AndroidUsbStates") == 2  # startup scan and background refresh
    assert HELPER.count("$usbStates = Get-AndroidUsbStates -Lines $script:LastConnectedUsbLines") == 2
    assert HELPER.count("$usbStates.MtpKeys.Contains(") == 2
    assert HELPER.count("@(@($usbStates.Ready) + @($usbStates.Rows))") == 2
    # Both device-list writers and the helper's own handlers map a needs-action phone and keep its guidance.
    assert HELPER.count('$adapter = "android_usb"; $status = [string]$d.state') == 2
    assert HELPER.count("guidance    = [string]$d.guidance") == 2
    assert '$adapterName = "android_usb"' in HELPER
    # A camera (picture transfer) or USB modem is not taken for a phone.
    assert HELPER.count("$notPhoneWords = ") == 2
    assert HELPER.count("($g.Phone -or $g.MsMtp)") == 2
    # The pnputil-style registry lines carry the manufacturer (phone words such as Samsung / Xiaomi).
    assert HELPER.count('$lines.Add("Manufacturer Name:          $mfg")') == 2


def test_diagnostic_script_is_read_only_ascii():
    text = (ROOT / "scripts" / "diagnose-phone-detection.ps1").read_bytes()
    assert all(b < 128 for b in text)
    src = text.decode("ascii")
    for forbidden in ("Stop-Process", ".Kill(", "Set-ItemProperty", "New-ItemProperty", "Restart-Service"):
        assert forbidden not in src
    assert "polaron-phone-detection.txt" in src
    assert "Get-AndroidUsbStates" in src
