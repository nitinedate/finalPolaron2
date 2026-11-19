#!/usr/bin/env python3
"""Live USB CLI probe. Never infer a connected phone from registry history.

ADB and libimobiledevice enumerate real currently attached transports, and query
model/product identifiers only when permitted. Unknown data stays unknown.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def find_tool(names: list[str]) -> str | None:
    for rel in names:
        candidate = REPO / rel
        if candidate.is_file():
            return str(candidate)
    for name in names:
        found = shutil.which(Path(name).name)
        if found:
            return found
    return None


def execute(command: list[str], seconds: float = 3) -> str:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=seconds,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def android_rows(adb: str | None) -> list[dict]:
    if not adb:
        return []
    devices = []
    for line in execute([adb, "devices", "-l"], 4).splitlines():
        match = re.match(r"^([^\s]+)\s+(device|unauthorized|offline)\b(.*)$", line.strip())
        if not match:
            continue
        serial, status, extras = match.groups()
        if status == "offline":
            continue
        authorized = status == "device"
        info = dict(re.findall(r"\b(model|product|device):([^\s]+)", extras))
        model = execute([adb, "-s", serial, "shell", "getprop", "ro.product.model"], 3) if authorized else ""
        manufacturer = execute([adb, "-s", serial, "shell", "getprop", "ro.product.manufacturer"], 3) if authorized else ""
        model = model or info.get("model", "").replace("_", " ")
        label = " ".join(x for x in (manufacturer, model) if x and x.lower() != model.lower())
        if model:
            label = (label + " " + model).strip() if label else model
        else:
            label = "Android phone (authorization required)" if not authorized else "Android phone (model unavailable)"
        devices.append({
            "id": serial, "name": label, "device_class": "ADB", "connection": "adb",
            "os_hint": "android", "status": status, "manufacturer": manufacturer,
            "model": model, "instance_id": "", "live_acquire": authorized,
            "guidance": "Unlock the phone and approve USB debugging" if not authorized else "USB debugging authorized",
        })
    return devices


def ios_rows(idevice_id: str | None, ideviceinfo: str | None) -> list[dict]:
    if not idevice_id:
        return []
    rows = []
    for udid in execute([idevice_id, "-l"], 4).splitlines():
        udid = udid.strip()
        if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", udid):
            continue
        product = execute([ideviceinfo, "-u", udid, "-k", "ProductType"], 3) if ideviceinfo else ""
        name = product if re.match(r"^(iPhone|iPad|iPod)[A-Za-z0-9,]+$", product) else "iOS device (model unavailable)"
        rows.append({
            "id": udid, "name": name, "device_class": "USBMUX", "connection": "usbmux",
            "os_hint": "ios", "model": product, "instance_id": "", "live_acquire": True,
            "guidance": "Unlock the device and approve Trust This Computer if prompted",
        })
    return rows


def main() -> int:
    adb = find_tool(["tools/platform-tools/adb.exe", "adb.exe", "adb"])
    ios_id = find_tool(["tools/libimobiledevice/idevice_id.exe", "idevice_id.exe", "idevice_id"])
    ios_info = find_tool(["tools/libimobiledevice/ideviceinfo.exe", "ideviceinfo.exe", "ideviceinfo"])
    rows = android_rows(adb) + ios_rows(ios_id, ios_info)
    print(json.dumps({"ok": True, "mobile_devices": rows, "count": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
