"""V1.31.4: an iPhone backup stuck at 100% never hangs the collection again.

pymobiledevice3 reads the backup connection with no timeout, so when the iPhone stops talking at the
end (locked/asleep while finishing, or a silently dead usbmux session) ios_usbmux_backup.py waited
forever and the collection sat at 65 %. The helper now watches for a stall and asks
``ios_usbmux_backup.py --verify`` whether the backup on disk is complete; a retry resumes into the
same staging folder (``--resume``) instead of starting a new full backup.
"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UDID = "00008150-0002659C0CBB401C"


def _load():
    spec = importlib.util.spec_from_file_location("ios_usbmux_backup_v1314", ROOT / "scripts" / "ios_usbmux_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mbfile(size: int) -> bytes:
    """Manifest.db 'file' column as iOS writes it: an NSKeyedArchiver MBFile."""
    archived = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null", {"Size": size, "Mode": 33188, "$class": plistlib.UID(2)}, {"$classname": "MBFile"}],
    }
    return plistlib.dumps(archived, fmt=plistlib.FMT_BINARY)


def make_backup(root: Path, files: dict[str, bytes], *, encrypted: bool = False) -> Path:
    backup = root / UDID
    backup.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(backup / "Manifest.db")
    con.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")
    con.execute("INSERT INTO Files VALUES (?,?,?,?,?)", ("direntry", "HomeDomain", "Library", 2, None))
    for file_id, data in files.items():
        con.execute("INSERT INTO Files VALUES (?,?,?,?,?)", (file_id, "AppDomain", file_id, 1, _mbfile(len(data))))
        (backup / file_id[:2]).mkdir(exist_ok=True)
        (backup / file_id[:2] / file_id).write_bytes(data)
    con.commit()
    con.execute("CREATE TABLE padding (x BLOB)")
    con.execute("INSERT INTO padding VALUES (?)", (b"x" * 256,))
    con.commit()
    con.close()
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": encrypted, "Version": "10.0", "Applications": {}}))
    (backup / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": "finished"}))
    return backup


FILES = {
    "d7af800f89fbaf26992a5ce327b35fdec2876a17": b"chat" * 100,
    "3d0d7e5fb2ce288813306e4d4636395e047a3d28": b"sms" * 50,
    "0a" + "1" * 38: b"",
}


def test_complete_backup_is_recognised(tmp_path):
    mod = _load()
    backup = make_backup(tmp_path, FILES)
    result = mod.verify_backup(backup, since=time.time() - 60)
    assert result["complete"] is True, result
    assert result["files_in_manifest"] == 3 and result["missing"] == 0 and result["truncated"] == 0


def test_missing_or_truncated_files_are_not_complete(tmp_path):
    mod = _load()
    backup = make_backup(tmp_path, FILES)
    (backup / "3d" / "3d0d7e5fb2ce288813306e4d4636395e047a3d28").unlink()
    result = mod.verify_backup(backup)
    assert result["complete"] is False and result["missing"] == 1
    assert result["missing_examples"] == ["3d0d7e5fb2ce288813306e4d4636395e047a3d28"]

    backup2 = make_backup(tmp_path / "b", FILES)
    (backup2 / "d7" / "d7af800f89fbaf26992a5ce327b35fdec2876a17").write_bytes(b"chat")
    result2 = mod.verify_backup(backup2)
    assert result2["complete"] is False and result2["truncated"] == 1


def test_encrypted_backup_sizes_are_not_compared(tmp_path):
    mod = _load()
    backup = make_backup(tmp_path, FILES, encrypted=True)
    (backup / "d7" / "d7af800f89fbaf26992a5ce327b35fdec2876a17").write_bytes(b"x")
    assert mod.verify_backup(backup)["complete"] is True


def test_session_must_have_written_the_manifest(tmp_path):
    mod = _load()
    backup = make_backup(tmp_path, FILES)
    old = time.time() - 3600
    os.utime(backup / "Manifest.db", (old, old))
    result = mod.verify_backup(backup, since=time.time() - 60)
    assert result["complete"] is False and result["reason"] == "manifest_db_older_than_this_session"


def test_start_of_session_files_are_not_a_finished_backup(tmp_path):
    mod = _load()
    backup = tmp_path / UDID
    backup.mkdir()
    (backup / "Manifest.plist").touch()  # what pymobiledevice3 writes when the backup starts
    assert mod.verify_backup(backup)["reason"] == "manifest_db_missing"
    (backup / "Manifest.db").write_bytes(b"not sqlite" * 20)
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False, "Version": "10.0", "x": "y" * 50}))
    assert mod.verify_backup(backup)["reason"].startswith("manifest_db_unreadable")
    (backup / "Manifest.plist").write_bytes(b"")
    assert mod.verify_backup(backup)["reason"] == "manifest_plist_empty"


def test_verify_command_line(tmp_path):
    import subprocess

    backup = make_backup(tmp_path, FILES)
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "ios_usbmux_backup.py"), "--verify", str(backup),
                          "--since", str(time.time() - 60)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    line = json.loads(run.stdout.strip().splitlines()[-1])
    assert line["stage"] == "verify" and line["complete"] is True
    (backup / "d7" / "d7af800f89fbaf26992a5ce327b35fdec2876a17").unlink()
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "ios_usbmux_backup.py"), "--verify", str(backup)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 2 and json.loads(run.stdout.strip().splitlines()[-1])["missing"] == 1


def _fake_pymobiledevice3(monkeypatch, calls: list):
    class Lockdown:
        def get_value(self, *a, **k):
            return {"TotalDataCapacity": 100, "TotalDataAvailable": 50}

        def close(self):
            calls.append("close")

    class Service:
        def __init__(self, lockdown):
            self.lockdown = lockdown

        def backup(self, full, backup_directory, progress_callback):
            calls.append(("backup", full, backup_directory))
            make_backup(Path(backup_directory), FILES) if not (Path(backup_directory) / UDID / "Manifest.db").exists() else None
            progress_callback(100)

    pkg = types.ModuleType("pymobiledevice3")
    lockdown_mod = types.ModuleType("pymobiledevice3.lockdown")
    lockdown_mod.create_using_usbmux = lambda serial=None: Lockdown()
    services = types.ModuleType("pymobiledevice3.services")
    mb2 = types.ModuleType("pymobiledevice3.services.mobilebackup2")
    mb2.Mobilebackup2Service = Service
    for name, mod in {
        "pymobiledevice3": pkg, "pymobiledevice3.lockdown": lockdown_mod,
        "pymobiledevice3.services": services, "pymobiledevice3.services.mobilebackup2": mb2,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    from app.services.mobile_acquire import ios_usbmux

    monkeypatch.setattr(ios_usbmux, "resolve_ios_udid", lambda value: UDID)


def test_resume_continues_the_earlier_staging_folder_without_a_full_backup(tmp_path, monkeypatch, capsys):
    mod = _load()
    calls: list = []
    _fake_pymobiledevice3(monkeypatch, calls)
    monkeypatch.chdir(tmp_path)  # "E:/ib/..." is a relative folder here
    earlier = Path("E:/ib") / "0CBB401C" / "20261116_180455"
    (earlier / UDID).mkdir(parents=True)
    (earlier / UDID / "Info.plist").write_bytes(b"x")
    monkeypatch.setattr(sys, "argv", ["x", "--udid", UDID, "--dest", str(tmp_path / "case" / "ios_image"),
                                      "--stamp", "20261116_180455", "--resume", "--required-free-bytes", "1"])
    assert mod.main() == 0
    backups = [c for c in calls if isinstance(c, tuple)]
    assert backups == [("backup", False, str(earlier))]
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert {"stage": "resume_staging", "dest": str(earlier), "requested_dest": str(tmp_path / "case" / "ios_image")} in out
    assert out[-1]["ok"] is True and out[-1]["backup_path"] == str(earlier / UDID)


def test_first_attempt_is_still_a_full_backup(tmp_path, monkeypatch):
    mod = _load()
    calls: list = []
    _fake_pymobiledevice3(monkeypatch, calls)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--udid", UDID, "--dest", str(tmp_path / "case" / "ios_image"), "--stamp", "20261116_190000", "--required-free-bytes", "1"])
    assert mod.main() == 0
    assert [c for c in calls if isinstance(c, tuple)][0][1] is True
