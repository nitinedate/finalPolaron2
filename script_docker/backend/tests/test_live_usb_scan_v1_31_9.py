"""V1.31.9: the workstation helper answers the phone lists from what Windows has plugged in now.

The helper's C# (inside host-drive-helper.ps1) is compiled with Mono and its classifier is fed the USB devices
Windows would report (instance IDs, descriptions, compatible IDs, started / problem state) for each case:
nothing plugged in, an iPhone, Android in each USB mode, and devices that are not phones.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "host-drive-helper.ps1"
MCS = shutil.which("mcs")
MONO = shutil.which("mono")

DRIVER = r'''
using System;
using System.Collections.Generic;
public static class Driver {
  static AetherisUsbNode N(string id, string desc, string ids, bool started, int problem) {
    AetherisUsbNode n = new AetherisUsbNode(); n.Id = id; n.Desc = desc; n.Ids = ids; n.Started = started; n.Problem = problem; return n;
  }
  static AetherisUsbNode N(string id, string desc, string ids) { return N(id, desc, ids, true, 0); }
  static void Case(string name, List<AetherisUsbNode> nodes) {
    AetherisUsbLive.Reset();
    AetherisUsbLive.Source = delegate { return nodes; };
    bool ok;
    List<AetherisPhone> phones = AetherisUsbLive.Phones(out ok);
    Console.WriteLine(name + "\t" + AetherisUsbLive.AcquisitionJson(phones) + "\t" + AetherisUsbLive.MobileJson(phones));
  }
  public static void Main() {
    Case("nothing", new List<AetherisUsbNode> {
      N(@"USB\ROOT_HUB30\4&1234&0&0", "USB Root Hub (USB 3.0)", @"USB\ROOT_HUB30"),
      N(@"USB\VID_046D&PID_C52B\5&2A&0&2", "USB Composite Device", @"USB\COMPOSITE"),
    });
    Case("iphone", new List<AetherisUsbNode> {
      N(@"USB\VID_05AC&PID_12A8\00008110001A2B3C4D5E801E", "Apple Mobile Device USB Composite Device", @"USB\COMPOSITE"),
      N(@"USB\VID_05AC&PID_12A8&MI_00\7&2F1&0&0000", "Apple iPhone", @"USB\Class_06&SubClass_01&Prot_01"),
      N(@"USB\VID_05AC&PID_12A8&MI_01\7&2F1&0&0001", "Apple Mobile Device USB Device", @"USB\Class_FF&SubClass_FE&Prot_02"),
    });
    Case("apple_keyboard", new List<AetherisUsbNode> {
      N(@"USB\VID_05AC&PID_024F\6&1&0&3", "USB Input Device", @"USB\Class_03&SubClass_01&Prot_01"),
    });
    Case("adb_and_mtp", new List<AetherisUsbNode> {
      N(@"USB\VID_2717&PID_FF48\a1b2c3d4", "USB Composite Device", @"USB\COMPOSITE"),
      N(@"USB\VID_2717&PID_FF48&MI_00\7&1F&0&0000", "Redmi Note 12", @"USB\MS_COMP_MTP USB\Class_06&SubClass_01&Prot_01"),
      N(@"USB\VID_2717&PID_FF48&MI_01\7&1F&0&0001", "ADB Device", @"USB\Class_ff&SubClass_42&Prot_01"),
    });
    Case("adb_only", new List<AetherisUsbNode> {
      N(@"USB\VID_18D1&PID_4EE7\8ABX0001", "ADB Device", @"USB\Class_ff&SubClass_42&Prot_01"),
    });
    Case("samsung_mtp", new List<AetherisUsbNode> {
      N(@"USB\VID_04E8&PID_6860\R5CW12ABCDE", "SAMSUNG Mobile USB Composite Device", @"USB\COMPOSITE"),
      N(@"USB\VID_04E8&PID_6860&MI_00\7&3A&0&0000", "SM-A546E", @"USB\MS_COMP_MTP USB\Class_06&SubClass_01&Prot_01"),
    });
    Case("charging_only", new List<AetherisUsbNode> {
      N(@"USB\VID_2717&PID_FF40\a1b2c3d4", "USB Composite Device", @"USB\COMPOSITE"),
    });
    Case("driver_missing", new List<AetherisUsbNode> {
      N(@"USB\VID_2717&PID_FF48\a1b2c3d4", "USB Composite Device", @"USB\COMPOSITE"),
      N(@"USB\VID_2717&PID_FF48&MI_01\7&1F&0&0001", "ADB Interface", @"USB\Class_ff&SubClass_42&Prot_01", false, 28),
    });
    Case("not_phones", new List<AetherisUsbNode> {
      N(@"USB\VID_04E8&PID_4001\0123456789AB", "Samsung Portable SSD T7", @"USB\Class_08&SubClass_06&Prot_62"),
      N(@"USB\VID_12D1&PID_1506&MI_00\7&2C&0&0000", "HUAWEI Mobile Connect - 3G Modem", @"USB\Class_ff&SubClass_02&Prot_01"),
      N(@"USB\VID_04A9&PID_32F1\0123ABCD", "Canon EOS R6", @"USB\Class_06&SubClass_01&Prot_01"),
    });
    Case("iphone_and_android", new List<AetherisUsbNode> {
      N(@"USB\VID_05AC&PID_12A8\00008110001A2B3C4D5E801E", "Apple Mobile Device USB Composite Device", @"USB\COMPOSITE"),
      N(@"USB\VID_04E8&PID_6860\R5CW12ABCDE", "SAMSUNG Mobile USB Composite Device", @"USB\COMPOSITE"),
      N(@"USB\VID_04E8&PID_6860&MI_00\7&3A&0&0000", "Galaxy \"S23\"", @"USB\MS_COMP_MTP"),
    });
    // A failing read falls back to the background file (ok = false).
    AetherisUsbLive.Reset();
    AetherisUsbLive.Source = delegate { throw new InvalidOperationException("setupapi unavailable"); };
    bool okFail;
    AetherisUsbLive.Phones(out okFail);
    Console.WriteLine("failure\t" + (okFail ? "ok" : "fallback") + "\t" + AetherisUsbLive.LastError);
  }
}
'''


def _helper_csharp() -> str:
    lines = HELPER.read_text(encoding="utf-8").split("\n")
    start = max(i for i, line in enumerate(lines) if line.startswith('Add-Type -TypeDefinition @"') and "AetherisHelperDispatcher" in "\n".join(lines[i:i + 2000]))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith('"@'))
    # PowerShell here-string: `$ becomes $.
    return "\n".join(lines[start + 1:end]).replace("`$", "$")


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    if not (MCS and MONO):
        pytest.skip("Mono (mcs/mono) not installed")
    work = tmp_path_factory.mktemp("livescan")
    (work / "helper.cs").write_text(_helper_csharp(), encoding="utf-8")
    (work / "driver.cs").write_text(DRIVER, encoding="utf-8")
    build = subprocess.run(
        [MCS, "-nowarn:0168,0219,0414,0649", "-out:" + str(work / "t.exe"), str(work / "helper.cs"), str(work / "driver.cs")],
        capture_output=True, text=True, timeout=300,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([MONO, str(work / "t.exe")], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    out = {}
    for line in run.stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "failure":
            out["failure"] = parts[1:]
        else:
            out[parts[0]] = (json.loads(parts[1]), json.loads(parts[2]))
    return out


def test_nothing_plugged_in_lists_nothing(cases):
    acq, mobile = cases["nothing"]
    assert acq["devices"] == [] and acq["count"] == 0 and acq["source"] == "live" and acq["scanned_utc"]
    assert mobile["mobile_devices"] == []


def test_iphone_is_listed_with_its_udid(cases):
    acq, mobile = cases["iphone"]
    assert len(acq["devices"]) == 1
    d = acq["devices"][0]
    assert d["adapter"] == "ios_lockdown" and d["os_family"] == "ios" and d["label"] == "Apple iPhone"
    # The collection reads the UDID from the end of this id (ConvertTo-IosUdid).
    assert d["device_id"] == "USB\\VID_05AC&PID_12A8\\00008110001A2B3C4D5E801E"
    assert mobile["mobile_devices"][0]["os_hint"] == "ios" and mobile["mobile_devices"][0]["live_acquire"] is True


def test_other_apple_usb_devices_are_not_phones(cases):
    assert cases["apple_keyboard"][0]["devices"] == []


def test_android_with_usb_debugging_is_one_phone_named_by_its_model(cases):
    acq, mobile = cases["adb_and_mtp"]
    assert len(acq["devices"]) == 1
    d = acq["devices"][0]
    assert d["adapter"] == "android_adb" and d["device_id"] == "a1b2c3d4" and d["label"] == "Redmi Note 12"
    assert mobile["mobile_devices"][0]["connection"] == "adb"


def test_adb_device_is_not_used_as_a_phone_name(cases):
    d = cases["adb_only"][0]["devices"][0]
    assert d["adapter"] == "android_adb" and d["label"] == "Android phone" and d["device_id"] == "8ABX0001"


def test_file_transfer_phone(cases):
    d = cases["samsung_mtp"][0]["devices"][0]
    assert d["adapter"] == "android_mtp" and d["label"] == "SM-A546E"
    assert d["device_id"] == "USB\\VID_04E8&PID_6860\\R5CW12ABCDE"


def test_charging_only_and_driver_missing_need_action(cases):
    charging = cases["charging_only"][0]["devices"][0]
    assert charging["adapter"] == "android_usb" and charging["state"] == "charging_only" and "File transfer" in charging["guidance"]
    assert cases["charging_only"][1]["mobile_devices"][0]["live_acquire"] is False
    missing = cases["driver_missing"][0]["devices"][0]
    assert missing["adapter"] == "android_usb" and missing["state"] == "driver_missing" and "driver" in missing["guidance"]


def test_ssd_modem_and_camera_are_not_phones(cases):
    assert cases["not_phones"][0]["devices"] == []


def test_two_phones_and_json_escaping(cases):
    acq, _ = cases["iphone_and_android"]
    assert [d["os_family"] for d in acq["devices"]] == ["ios", "android"]
    assert acq["devices"][1]["label"] == 'Galaxy "S23"'


def test_a_failed_read_falls_back_to_the_background_list(cases):
    assert cases["failure"] == ["fallback", "setupapi unavailable"]


def test_endpoints_use_the_live_read():
    text = HELPER.read_text(encoding="utf-8")
    assert 'path == "/acquisition/devices") WriteLiveDevices(ctx, query);' in text
    assert 'WriteLiveMobile(ctx, query);' in text
    assert "SetupDiGetClassDevsW(IntPtr.Zero, \"USB\", IntPtr.Zero, DIGCF_PRESENT | DIGCF_ALLCLASSES)" in text
    assert 'HelperVersion = "5.66"' in text
