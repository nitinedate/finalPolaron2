"""V1.31.14 - a collection on an examiner computer is sent to the forensic server while it runs.

Covers without Windows:
* the upload session (state next to the client copies, token only as a hash, one case / one run),
* chunks, commits, small-file bundles, resend of a changed file (discard), finish and abort, against an
  in-process stand-in for the server HostDrive helper writer (same rules as the C# writer),
* the registered run: server paths, hash list, provenance, sealed original extraction,
* the HTTP routes (token-only, no user sign-in),
* end to end: scripts/polaron-stream-upload.ps1 (pwsh) streaming a running collection to a live server,
  then removing the local copy only after the server confirmed every file,
* the helper hand-off: destination pinned to client-kit.json, token kept out of the payload file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "host-drive-helper.ps1"
UPLOADER = REPO / "scripts" / "polaron-stream-upload.ps1"
PWSH = shutil.which("pwsh") or str(Path.home() / ".local/pwsh/pwsh")
SERVER_CASES = "D:\\Polaron\\evidence\\cases"
RUN = "CASE-7_E01_SM-A515F_LOGICAL_20261118_101500"
RUN_FOLDERS = {"02_Original_Extraction", "03_Working_Copy", "05_Exports", "06_Reports", "07_Logs", "08_Hashes"}


# ---------------------------------------------------------------------------
# Stand-in for the server HostDrive helper's writer (/acquisition/import/*)
# ---------------------------------------------------------------------------

class FakeWriter:
    def __init__(self, cases_root: Path):
        self.cases_root = cases_root
        self.calls: list[str] = []
        self.chunk_rels: list[str] = []
        writer = self

        def safe(seg: str) -> bool:
            return bool(seg) and seg not in (".", "..") and not any(ord(c) < 32 or c in '<>:"|?*\\/' for c in seg) \
                and not seg.lower().endswith(".polaron-part")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _rel(self, raw=None):
                if raw is None:
                    raw = self.headers.get("X-Import-Rel") or ""
                    raw = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
                parts = raw.split("/")
                if len(parts) < 3 or parts[0] not in RUN_FOLDERS or not all(safe(p) for p in parts):
                    raise ValueError(f"bad rel {raw}")
                return raw

            def do_POST(self):
                writer.calls.append(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                case_id = self.headers.get("X-Import-Case-Id") or ""
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", case_id):
                    return self._send(400, {"ok": False, "error": "bad_request"})
                case_dir = writer.cases_root / case_id
                win_case = SERVER_CASES + "\\" + case_id
                try:
                    if self.path == "/acquisition/import/prepare":
                        for sub in ("01_Authority", "02_Original_Extraction", "03_Working_Copy", "04_PA_Case", "05_Exports",
                                    "06_Reports", "07_Logs", "08_Hashes", "09_Review", "10_Disclosure"):
                            (case_dir / sub).mkdir(parents=True, exist_ok=True)
                        return self._send(200, {"ok": True, "case_dir": win_case, "existing": [], "conflicts": []})
                    if self.path == "/acquisition/import/chunk":
                        rel = self._rel()
                        writer.chunk_rels.append(rel)
                        offset = int(self.headers.get("X-Import-Offset") or 0)
                        final = case_dir / rel
                        if final.exists():
                            return self._send(409, {"ok": False, "error": "exists", "size": final.stat().st_size})
                        part = Path(str(final) + ".polaron-part")
                        part.parent.mkdir(parents=True, exist_ok=True)
                        if offset == 0:
                            part.write_bytes(b"")
                        elif not part.exists() or part.stat().st_size != offset:
                            return self._send(409, {"ok": False, "error": "offset_mismatch", "size": part.stat().st_size if part.exists() else 0})
                        with open(part, "ab") as fh:
                            fh.write(body)
                        return self._send(200, {"ok": True, "size": part.stat().st_size})
                    if self.path == "/acquisition/import/commit":
                        rel = self._rel()
                        size = int(self.headers["X-Import-Size"])
                        expected = self.headers["X-Import-Sha256"].lower()
                        final = case_dir / rel
                        part = Path(str(final) + ".polaron-part")
                        if final.exists():
                            sha = hashlib.sha256(final.read_bytes()).hexdigest()
                            part.unlink(missing_ok=True)
                            if final.stat().st_size == size and sha == expected:
                                return self._send(200, {"ok": True, "exists": True, "sha256": sha, "path": win_case})
                            return self._send(409, {"ok": False, "error": "conflict", "sha256": sha})
                        if not part.exists():
                            return self._send(409, {"ok": False, "error": "no_part"})
                        if part.stat().st_size != size:
                            part.unlink()
                            return self._send(422, {"ok": False, "error": "size_mismatch"})
                        sha = hashlib.sha256(part.read_bytes()).hexdigest()
                        if sha != expected:
                            part.unlink()
                            return self._send(422, {"ok": False, "error": "hash_mismatch", "sha256": sha})
                        part.rename(final)
                        return self._send(200, {"ok": True, "exists": False, "sha256": sha, "path": win_case + "\\" + rel.replace("/", "\\")})
                    if self.path == "/acquisition/import/seal":
                        rels = [r for r in body.decode().splitlines() if r.startswith("02_Original_Extraction/")]
                        for rel in rels:
                            (case_dir / rel).chmod(0o444)
                        return self._send(200, {"ok": True, "sealed": len(rels), "failed": 0})
                    if self.path == "/acquisition/import/discard":
                        removed = kept = 0
                        for line in body.decode().splitlines():
                            sha, rel = line.split("\t", 1)
                            final = case_dir / self._rel(rel)
                            if not final.is_file():
                                continue
                            if not (final.stat().st_mode & 0o222) or hashlib.sha256(final.read_bytes()).hexdigest() != sha:
                                kept += 1
                                continue
                            final.unlink()
                            removed += 1
                        return self._send(200, {"ok": kept == 0, "removed": removed, "kept": kept})
                except ValueError as exc:
                    return self._send(400, {"ok": False, "error": "bad_request", "detail": str(exc)})
                return self._send(404, {"ok": False, "error": "not_found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def env(tmp_path, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    from app.services.client_evidence import registry
    from app.services.mobile_acquire import host_bridge

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(registry, "_redis", lambda: redis)
    server_cases = tmp_path / "server" / "evidence" / "cases"
    server_cases.mkdir(parents=True)
    writer = FakeWriter(server_cases)
    health = {"ok": True, "client_import": True, "computer_name": "FORENSIC-SRV", "helper_version": "5.66",
              "import_case_root": SERVER_CASES, "root": "D:\\Polaron"}
    monkeypatch.setattr(host_bridge, "helper_reachable", lambda *a, **kw: True)
    monkeypatch.setattr(host_bridge, "helper_health", lambda: dict(health))
    monkeypatch.setattr(host_bridge, "helper_base_url", lambda: writer.url)
    monkeypatch.setattr(host_bridge, "helper_auth_headers", lambda url=None: {})
    yield {"server": server_cases, "writer": writer, "redis": redis, "health": health, "tmp": tmp_path}
    writer.close()


def _session(**kw):
    from app.services.mobile_acquire import client_stream

    args = dict(case_id="CASE-7", examiner="examiner@lab", platform="android", service="mobile-android",
                client_host="EXAMINER-PC-07")
    args.update(kw)
    return client_stream.create_session(**args)


def _put(sid, token, rel, data: bytes, chunk=4096):
    from app.services.mobile_acquire import client_stream

    offset = 0
    while True:
        part = data[offset:offset + chunk]
        status, body = client_stream.put_chunk(sid, token, rel, offset, part)
        assert status == 200, body
        offset += len(part)
        if offset >= len(data):
            break
    return client_stream.commit_file(sid, token, rel=rel, size=len(data), sha256=hashlib.sha256(data).hexdigest())


# ---------------------------------------------------------------------------
# Session and upload
# ---------------------------------------------------------------------------

def test_session_lives_with_client_copies_and_keeps_only_a_token_hash(env):
    from app.services.mobile_acquire import client_import, client_stream

    out = _session(keep_local=True)
    sid, token = out["session_id"], out["token"]
    assert re.fullmatch(r"[0-9a-f]{20}", sid) and len(token) >= 40
    assert out["chunk_bytes"] == 8 * 1024 * 1024 and out["service"] == "mobile-android"
    state = client_import.load_state(sid)
    assert state["kind"] == "stream" and state["status"] == "waiting" and state["transport"] == "client_stream"
    assert state["server_case_dir"] == SERVER_CASES + "\\CASE-7" and state["keep_local"] is True
    assert token not in json.dumps(state) and state["_token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    public = client_import.public_state(state)
    assert "_token_sha256" not in public and token not in json.dumps(public)
    assert (env["server"] / "CASE-7" / "02_Original_Extraction").is_dir(), "case skeleton is created on the server"
    with pytest.raises(client_stream.StreamError) as wrong:
        client_stream.session_status(sid, "x" * 43)
    assert wrong.value.status == 401


def test_session_needs_the_server_helper(env, monkeypatch):
    from app.services.mobile_acquire import client_stream, host_bridge

    monkeypatch.setattr(host_bridge, "helper_reachable", lambda *a, **kw: False)
    with pytest.raises(client_stream.StreamError) as off:
        _session()
    assert off.value.status == 503 and off.value.code == "server_helper_offline"


def test_chunks_commit_and_paths_are_checked(env):
    from app.services.mobile_acquire import client_import, client_stream

    out = _session()
    sid, token = out["session_id"], out["token"]
    data = os.urandom(10_000)
    rel = f"02_Original_Extraction/{RUN}/sdcard/DCIM/IMG_0001.jpg"
    res = _put(sid, token, rel, data)
    assert res["ok"] and res["sha256"] == hashlib.sha256(data).hexdigest()
    assert (env["server"] / "CASE-7" / rel).read_bytes() == data
    state = client_import.load_state(sid)
    assert state["status"] == "running" and state["run_name"] == RUN and state["files_done"] == 1 and state["bytes_done"] == 10_000

    # A second run, a path outside the run folders and a traversal are refused.
    for bad, status in (("02_Original_Extraction/OTHER_RUN/x.bin", 409), ("01_Authority/a/b.pdf", 400),
                        (f"02_Original_Extraction/{RUN}/../../x", 400), (f"02_Original_Extraction/{RUN}/a.polaron-part", 400)):
        with pytest.raises(client_stream.StreamError) as err:
            client_stream.put_chunk(sid, token, bad, 0, b"x")
        assert err.value.status == status, bad

    # Resume: an offset that does not match tells the client where the server is.
    rel2 = f"02_Original_Extraction/{RUN}/big.bin"
    status, body = client_stream.put_chunk(sid, token, rel2, 0, b"a" * 100)
    assert status == 200
    status, body = client_stream.put_chunk(sid, token, rel2, 500, b"b" * 100)
    assert status == 409 and body == {"ok": False, "error": "offset_mismatch", "size": 100}
    # A wrong SHA-256 is refused by the server disk check; the client sends the file again.
    with pytest.raises(client_stream.StreamError) as bad_hash:
        client_stream.commit_file(sid, token, rel=rel2, size=100, sha256="0" * 64)
    assert bad_hash.value.status == 422
    # Committing the same verified file again is harmless; a different file at that path is a conflict.
    again = client_stream.commit_file(sid, token, rel=rel, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    assert again["ok"] and again["exists"]
    with pytest.raises(client_stream.StreamError) as conflict:
        client_stream.commit_file(sid, token, rel=rel, size=len(data), sha256="1" * 64)
    assert conflict.value.status == 409 and conflict.value.code == "conflict"
    status_out = client_stream.session_status(sid, token)
    assert status_out["files"] == {rel: [10_000, hashlib.sha256(data).hexdigest()]}


def test_small_files_travel_in_bundles(env):
    from app.services.mobile_acquire import client_import, client_stream

    out = _session()
    sid, token = out["session_id"], out["token"]
    files = {f"02_Original_Extraction/{RUN}/sms/{i:03d}.txt": os.urandom(100 + i) for i in range(40)}
    files[f"02_Original_Extraction/{RUN}/empty.txt"] = b""
    items = [{"rel": rel, "size": len(d), "sha256": hashlib.sha256(d).hexdigest(), "mtime_ms": 1} for rel, d in files.items()]
    items[3]["sha256"] = "f" * 64  # corrupted on the way
    body = json.dumps({"files": items, "total_files": 41, "total_bytes": sum(len(d) for d in files.values())}).encode() + b"\n" + b"".join(files.values())
    res = client_stream.put_bundle(sid, token, body)
    bad = [r for r in res["results"] if not r["ok"]]
    assert len(bad) == 1 and bad[0]["rel"] == items[3]["rel"] and bad[0]["error"] == "hash_mismatch"
    for rel, data in files.items():
        if rel != items[3]["rel"]:
            assert (env["server"] / "CASE-7" / rel).read_bytes() == data
    state = client_import.load_state(sid)
    assert state["files_done"] == 40 and state["total_files"] == 41
    with pytest.raises(client_stream.StreamError) as short:
        client_stream.put_bundle(sid, token, body[:-5])
    assert short.value.code == "short_body"


def test_changed_file_is_replaced_only_before_the_run_is_registered(env):
    from app.services.mobile_acquire import client_stream

    out = _session()
    sid, token = out["session_id"], out["token"]
    rel = f"02_Original_Extraction/{RUN}/Library/notes.sqlite"
    _put(sid, token, rel, b"first version")
    assert client_stream.discard_file(sid, token, rel)["removed"] == 1
    assert not (env["server"] / "CASE-7" / rel).exists()
    _put(sid, token, rel, b"second, longer version")
    assert (env["server"] / "CASE-7" / rel).read_bytes() == b"second, longer version"
    assert client_stream.session_status(sid, token)["files"][rel][0] == len(b"second, longer version")


def test_finish_registers_the_run_on_the_server(env):
    from app.services.mobile_acquire import client_import, client_stream

    out = _session()
    sid, token = out["session_id"], out["token"]
    client_case = "C:\\evidence\\CASE-7"
    summary = {
        "ok": True, "run_name": RUN, "device": {"os_family": "android", "model": "SM-A515F"},
        "paths": {"case_root": client_case, "original": client_case + "\\02_Original_Extraction\\" + RUN},
        "evidence_package": {"run_name": RUN, "collection_summary": client_case + "\\07_Logs\\" + RUN + "\\collection_summary.json"},
    }
    files = {
        f"02_Original_Extraction/{RUN}/sdcard/a.jpg": os.urandom(5000),
        f"03_Working_Copy/{RUN}/readable/contacts.csv": b"a,1\n",
        f"07_Logs/{RUN}/collection_summary.json": json.dumps(summary).encode(),
    }
    listed = []
    for rel, data in list(files.items())[:2]:
        _put(sid, token, rel, data)
        listed.append({"rel": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    last_rel, last = list(files.items())[2]
    listed.append({"rel": last_rel, "size": len(last), "sha256": hashlib.sha256(last).hexdigest()})

    missing = client_stream.finish(sid, token, run_name=RUN, files=listed, summary=summary, client_case_dir=client_case)
    assert missing["ok"] is False and missing["missing"] == [last_rel]
    _put(sid, token, last_rel, last)
    done = client_stream.finish(sid, token, run_name=RUN, files=listed, summary=summary,
                                client_case_dir=client_case, client_host="EXAMINER-PC-07")
    assert done["ok"] and done["server_case_dir"] == SERVER_CASES + "\\CASE-7"

    state = client_import.load_state(sid)
    assert state["status"] == "completed" and state["run"]["client_import"] is True
    result = state["run"]["result"]
    assert result["client_import"]["transport"] == "client_stream"
    assert result["paths"]["original"] == SERVER_CASES + "\\CASE-7\\02_Original_Extraction\\" + RUN
    assert result["evidence_package"]["collection_summary"].startswith(SERVER_CASES)
    assert state["run"]["adapter"] == "android_mtp"
    case = env["server"] / "CASE-7"
    hash_list = (case / "08_Hashes" / RUN / f"client_stream_{sid}_sha256.txt").read_text()
    for item in listed:
        assert f"{item['sha256']}  {item['rel']}" in hash_list
    prov = json.loads((case / "07_Logs" / RUN / f"client_stream_{sid}.json").read_text())
    assert prov["client"]["hostname"] == "EXAMINER-PC-07" and prov["files"]["count"] == 3
    assert not ((case / listed[0]["rel"]).stat().st_mode & 0o222), "original extraction is sealed read-only"
    # Sealed: a later discard cannot touch it; finishing again is idempotent.
    assert client_stream.finish(sid, token, run_name=RUN, files=listed)["already"] is True
    with pytest.raises(client_stream.StreamError):
        client_stream.discard_file(sid, token, listed[0]["rel"])


def test_abort_and_examiner_cancel(env):
    from app.services.mobile_acquire import client_import, client_stream

    out = _session()
    sid, token = out["session_id"], out["token"]
    assert client_import.request_cancel(sid)
    with pytest.raises(client_stream.StreamError) as stopped:
        client_stream.put_chunk(sid, token, f"02_Original_Extraction/{RUN}/a.bin", 0, b"x")
    assert stopped.value.code == "cancelled" and client_import.load_state(sid)["status"] == "cancelled"

    out = _session()
    res = client_stream.abort(out["session_id"], out["token"], "USB cable pulled")
    state = client_import.load_state(out["session_id"])
    assert res["ok"] and state["status"] == "failed" and "USB cable" in state["error"]


# ---------------------------------------------------------------------------
# HTTP routes (token only)
# ---------------------------------------------------------------------------

def _app():
    from fastapi import FastAPI

    from app.routers import acquisition

    app = FastAPI()
    app.include_router(acquisition.router)
    return app


def test_http_routes(env):
    from fastapi.testclient import TestClient

    out = _session()
    sid, token = out["session_id"], out["token"]
    client = TestClient(_app())
    base = f"/api/acquisition/client-stream/{sid}"
    rel = f"02_Original_Extraction/{RUN}/x.bin"
    hdr = {"X-Polaron-Stream-Token": token, "X-Stream-Rel": base64.b64encode(rel.encode()).decode(), "X-Stream-Offset": "0"}
    assert client.put(base + "/chunk", content=b"hello", headers={**hdr, "X-Polaron-Stream-Token": "nope" * 8}).status_code == 401
    r = client.put(base + "/chunk", content=b"hello", headers=hdr)
    assert r.status_code == 200 and r.json() == {"ok": True, "size": 5}
    r = client.post(base + "/commit", json={"rel": rel, "size": 5, "sha256": hashlib.sha256(b"hello").hexdigest()},
                    headers={"X-Polaron-Stream-Token": token})
    assert r.status_code == 200 and r.json()["ok"]
    r = client.get(base + "/status?format=tsv", headers={"X-Polaron-Stream-Token": token})
    lines = r.text.strip().split("\n")
    assert lines[0].startswith("#status\trunning\t" + RUN) and lines[1] == f"5\t{hashlib.sha256(b'hello').hexdigest()}\t{rel}"
    r = client.post(base + "/finish", json={"run_name": RUN, "files": [{"rel": rel, "size": 5}]},
                    headers={"X-Polaron-Stream-Token": token})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get(f"/api/acquisition/client-stream/{'0' * 20}/status", headers={"X-Polaron-Stream-Token": token}).status_code == 404


# ---------------------------------------------------------------------------
# End to end with the client uploader (pwsh)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(env):
    uvicorn = pytest.importorskip("uvicorn")
    port = _free_port()
    config = uvicorn.Config(_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


JOB_SCRIPT = r'''
param([string]$Case, [string]$Run, [string]$Progress, [string]$Result, [string]$Fail, [string]$Release)
function Wait-Release([string]$Name) {
    # The test lets the collection go on once it saw what it checks (no timing guesses).
    for ($i = 0; $i -lt 900 -and -not (Test-Path -LiteralPath ($Release + $Name)); $i++) { Start-Sleep -Milliseconds 100 }
}
$orig = Join-Path (Join-Path $Case "02_Original_Extraction") $Run
$logs = Join-Path (Join-Path $Case "07_Logs") $Run
New-Item -ItemType Directory -Force -Path $orig, $logs | Out-Null
'{"stage":"acquire","run_name":"' + $Run + '"}' | Set-Content -LiteralPath $Progress
$rnd = New-Object Random 7
$big = New-Object byte[] (3MB + 4567); $rnd.NextBytes($big)
[IO.File]::WriteAllBytes((Join-Path $orig "DCIM_IMG_0001.mp4"), $big)
New-Item -ItemType Directory -Force -Path (Join-Path $orig "sms") | Out-Null
for ($i = 0; $i -lt 30; $i++) { [IO.File]::WriteAllText((Join-Path $orig ("sms\m" + $i + ".txt")), ("message " + $i)) }
Wait-Release "1"
# Written while the collection still runs, then rewritten: must reach the server in its final form.
[IO.File]::WriteAllText((Join-Path $orig "Manifest.db"), "partial")
Start-Sleep -Seconds 3
[IO.File]::WriteAllText((Join-Path $orig "Manifest.db"), "complete manifest database")
[IO.File]::WriteAllText((Join-Path $logs "collection_summary.json"), '{"ok":true,"run_name":"' + $Run + '","device":{"os_family":"android"}}')
if ($Fail -eq "1") { '{"ok":false,"run_name":"' + $Run + '","error":"cancelled"}' | Set-Content -LiteralPath $Result; exit 1 }
'{"ok":true,"run_name":"' + $Run + '","stage_reached":"complete"}' | Set-Content -LiteralPath $Result
'''


IOS_JOB_SCRIPT = r'''
param([string]$Case, [string]$Run, [string]$Progress, [string]$Result, [string]$Fail, [string]$Release)
function Wait-Release([string]$Name) {
    for ($i = 0; $i -lt 900 -and -not (Test-Path -LiteralPath ($Release + $Name)); $i++) { Start-Sleep -Milliseconds 100 }
}
$orig = Join-Path (Join-Path $Case "02_Original_Extraction") $Run
$logs = Join-Path (Join-Path $Case "07_Logs") $Run
New-Item -ItemType Directory -Force -Path $orig, $logs | Out-Null
'{"stage":"acquire","run_name":"' + $Run + '"}' | Set-Content -LiteralPath $Progress
$stamp = $Run.Substring($Run.Length - 15)
# pymobiledevice3 writes the backup to <drive>:\ib\<id>\<stamp>\<udid> (here: $env:TEMP/ib/...) first.
$stage = Join-Path (Join-Path (Join-Path $env:TEMP "ib") "4F2A9C01") $stamp
$udid = Join-Path $stage "00008110-001A2B3C4D5E6F70"
New-Item -ItemType Directory -Force -Path (Join-Path $udid "3d"), (Join-Path $udid "a1") | Out-Null
[IO.File]::WriteAllText((Join-Path $udid "3d\3d0d7e5fb2ce288813306e4d4636395e047a3d28"), "sms database")
[IO.File]::WriteAllBytes((Join-Path $udid "a1\a1b2c3"), (New-Object byte[] (2MB + 11)))
[IO.File]::WriteAllText((Join-Path $orig "notes.txt"), "first version")
Wait-Release "1"
# Changed after it was sent: the server copy is replaced before the run is registered.
[IO.File]::WriteAllText((Join-Path $orig "notes.txt"), "second version, longer")
[IO.File]::WriteAllText((Join-Path $udid "Manifest.db"), "manifest")
Start-Sleep -Seconds 5
# Backup complete: moved into the case (same volume), as Link-IosBackupIntoJob does.
$dest = Join-Path $orig "ios_image"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Move-Item -LiteralPath $udid -Destination (Join-Path $dest "00008110-001A2B3C4D5E6F70")
'{"ok":true,"run_name":"' + $Run + '","stage_reached":"complete","device":{"os_family":"ios"}}' | Set-Content -LiteralPath $Result
'''


def release(tmp: Path) -> None:
    """Lets the stand-in collection go on to its end."""
    (tmp / "release1").write_text("go")


def _start_job(tmp: Path, server_url: str, sid: str, token: str, *, keep_local=False, fail=False, ios=False, hold=True):
    jobs = tmp / "jobs"
    jobs.mkdir(exist_ok=True)
    case_root = tmp / "client_evidence"
    case = case_root / "CASE-7"
    progress, result = tmp / "progress.json", tmp / "result.json"
    payload = tmp / "payload.json"
    payload.write_text(json.dumps({"case_id": "CASE-7", "case_root": str(case_root), "stream_token": ""}))
    script = tmp / "job.ps1"
    script.write_text(IOS_JOB_SCRIPT if ios else JOB_SCRIPT)
    run_env = {**os.environ, "TEMP": str(tmp / "temp")}
    (tmp / "temp").mkdir(exist_ok=True)
    if not hold:
        release(tmp)
    job = subprocess.Popen([PWSH, "-NoProfile", "-File", str(script), "-Case", str(case), "-Run", RUN,
                            "-Progress", str(progress), "-Result", str(result), "-Fail", "1" if fail else "0",
                            "-Release", str(tmp / "release")], env=run_env)
    threading.Thread(target=job.wait, daemon=True).start()  # reap it: a zombie still shows in Get-Process
    job_id = uuid.uuid4().hex  # the uploader's single-instance lock is per job id
    stream_file = jobs / f"{job_id}.stream.json"
    stream_file.write_text(json.dumps({
        "job_id": job_id, "session_id": sid, "token": token, "server": server_url, "service": "mobile-android",
        "keep_local": keep_local, "platform": "ios" if ios else "android", "pid": job.pid, "payload_file": str(payload),
        "result_file": str(result), "progress_file": str(progress), "root": str(tmp),
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    up = subprocess.Popen([PWSH, "-NoProfile", "-File", str(UPLOADER), "-StreamFile", str(stream_file),
                           "-PollSec", "1", "-StableSec", "2", "-ChunkMB", "1", "-SmallFileKB", "64", "-GraceSec", "3"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=run_env)
    return job, up, case, jobs, job_id


@pytest.mark.skipif(not Path(PWSH).exists(), reason="pwsh not installed")
def test_uploader_streams_a_running_collection_and_moves_it(env, live_server):
    from app.services.mobile_acquire import client_import

    out = _session()
    sid, token = out["session_id"], out["token"]
    job, up, case, jobs, job_id = _start_job(env["tmp"], live_server, sid, token)
    # Files reach the server while the collection still runs.
    big_on_server = env["server"] / "CASE-7" / "02_Original_Extraction" / RUN / "DCIM_IMG_0001.mp4"
    deadline = time.time() + 60
    while time.time() < deadline and not big_on_server.exists():
        time.sleep(0.2)
    assert big_on_server.exists() and job.poll() is None, "the big file was sent before the collection ended"
    release(env["tmp"])
    stdout, stderr = up.communicate(timeout=180)
    job.wait(timeout=30)
    assert up.returncode == 0, stdout + stderr + (jobs / f"{job_id}.stream.log").read_text()
    status = json.loads((jobs / f"{job_id}.stream.status.json").read_text())
    log = (jobs / f"{job_id}.stream.log").read_text()
    assert status["state"] == "completed" and status["deleted_local"] is True, (status, log)
    srv = env["server"] / "CASE-7"
    assert (srv / "02_Original_Extraction" / RUN / "Manifest.db").read_text() == "complete manifest database"
    assert len(list((srv / "02_Original_Extraction" / RUN / "sms").iterdir())) == 30
    hash_list = (srv / "08_Hashes" / RUN / f"client_stream_{sid}_sha256.txt").read_text()
    big_rel = f"02_Original_Extraction/{RUN}/DCIM_IMG_0001.mp4"
    assert f"{hashlib.sha256(big_on_server.read_bytes()).hexdigest()}  {big_rel}" in hash_list
    assert big_on_server.stat().st_size == 3 * 1024 * 1024 + 4567
    state = client_import.load_state(sid)
    assert state["status"] == "completed" and state["total_files"] == 33, state
    # The local copy is gone, the receipt stays, the token is wiped from the stream file.
    assert not (case / "02_Original_Extraction" / RUN).exists() and not (case / "07_Logs" / RUN).exists()
    receipt = json.loads((case / f"MOVED_TO_SERVER_{RUN}.json").read_text())
    assert receipt["files"] == 33 and receipt["server_case_dir"] == SERVER_CASES + "\\CASE-7"
    assert receipt["hash_list_on_server"].startswith(SERVER_CASES + "\\CASE-7\\08_Hashes\\")
    assert json.loads((jobs / f"{job_id}.stream.json").read_text())["token"] == ""
    assert (jobs / f"{job_id}.stream.done").read_text() == "completed"


@pytest.mark.skipif(not Path(PWSH).exists(), reason="pwsh not installed")
def test_uploader_sends_the_iphone_backup_from_staging_and_resends_changed_files(env, live_server):
    from app.services.mobile_acquire import client_import

    out = _session(platform="ios", service="mobile-ios")
    sid = out["session_id"]
    job, up, case, jobs, job_id = _start_job(env["tmp"], live_server, sid, out["token"], ios=True)
    rel_sms = f"02_Original_Extraction/{RUN}/ios_image/00008110-001A2B3C4D5E6F70/3d/3d0d7e5fb2ce288813306e4d4636395e047a3d28"
    srv = env["server"] / "CASE-7"
    notes = srv / f"02_Original_Extraction/{RUN}/notes.txt"
    deadline = time.time() + 60
    while time.time() < deadline and not ((srv / rel_sms).exists() and notes.exists()):
        time.sleep(0.2)
    assert (srv / rel_sms).exists() and job.poll() is None, "sent from the staging folder while the backup ran"
    assert notes.read_text() == "first version"
    release(env["tmp"])
    stdout, stderr = up.communicate(timeout=180)
    log = (jobs / f"{job_id}.stream.log").read_text()
    assert up.returncode == 0, stdout + stderr + log
    assert (srv / f"02_Original_Extraction/{RUN}/notes.txt").read_text() == "second version, longer"
    assert "Replacing the server copy of a file that changed here: 02_Original_Extraction/" + RUN + "/notes.txt" in log
    assert log.count("Replacing the server copy") == 1, "the backup moved into the case is not sent again"
    assert (srv / f"02_Original_Extraction/{RUN}/ios_image/00008110-001A2B3C4D5E6F70/Manifest.db").read_text() == "manifest"
    state = client_import.load_state(sid)
    assert state["status"] == "completed" and state["run"]["adapter"] == "ios_lockdown"
    assert not (case / "02_Original_Extraction" / RUN).exists()
    assert not (env["tmp"] / "temp" / "ib" / "4F2A9C01" / RUN[-15:]).exists(), "empty staging folder removed"


@pytest.mark.skipif(not Path(PWSH).exists(), reason="pwsh not installed")
def test_uploader_restart_continues_from_what_the_server_holds(env, live_server):
    from app.services.mobile_acquire import client_import

    out = _session()
    sid = out["session_id"]
    job, up, case, jobs, job_id = _start_job(env["tmp"], live_server, sid, out["token"])
    big = env["server"] / "CASE-7" / "02_Original_Extraction" / RUN / "DCIM_IMG_0001.mp4"
    deadline = time.time() + 60
    while time.time() < deadline and not big.exists():
        time.sleep(0.2)
    assert big.exists()
    up.kill()  # computer restarted / uploader stopped
    up.wait(timeout=10)
    release(env["tmp"])
    big_rel = f"02_Original_Extraction/{RUN}/DCIM_IMG_0001.mp4"
    big_chunks_before = env["writer"].chunk_rels.count(big_rel)
    again = subprocess.Popen([PWSH, "-NoProfile", "-File", str(UPLOADER), "-StreamFile", str(jobs / f"{job_id}.stream.json"),
                              "-PollSec", "1", "-StableSec", "2", "-ChunkMB", "1", "-SmallFileKB", "64", "-GraceSec", "3"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**os.environ, "TEMP": str(env["tmp"] / "temp")})
    stdout, stderr = again.communicate(timeout=180)
    log = (jobs / f"{job_id}.stream.log").read_text()
    assert again.returncode == 0, stdout + stderr + log
    assert "Server already holds" in log
    assert client_import.load_state(sid)["status"] == "completed"
    # The 3 MiB file (1 MiB chunks) was verified on the server before the restart and is not sent again.
    assert big_chunks_before == 4 and env["writer"].chunk_rels.count(big_rel) == 4


@pytest.mark.skipif(not Path(PWSH).exists(), reason="pwsh not installed")
def test_uploader_keeps_the_local_copy_when_asked_or_when_the_collection_fails(env, live_server):
    from app.services.mobile_acquire import client_import

    out = _session(keep_local=True)
    (env["tmp"] / "keep").mkdir()
    job, up, case, jobs, job_id = _start_job(env["tmp"] / "keep", live_server, out["session_id"], out["token"], keep_local=True,
                                             hold=False)
    up.communicate(timeout=180)
    job.wait(timeout=30)
    assert client_import.load_state(out["session_id"])["status"] == "completed"
    local_big = case / "02_Original_Extraction" / RUN / "DCIM_IMG_0001.mp4"
    server_big = env["server"] / "CASE-7" / "02_Original_Extraction" / RUN / "DCIM_IMG_0001.mp4"
    assert local_big.read_bytes() == server_big.read_bytes(), "kept as the examiner asked, identical on the server"

    out = _session()
    (env["tmp"] / "fail").mkdir()
    job, up, case, jobs, job_id = _start_job(env["tmp"] / "fail", live_server, out["session_id"], out["token"], fail=True,
                                             hold=False)
    up.communicate(timeout=180)
    job.wait(timeout=30)
    status = json.loads((jobs / f"{job_id}.stream.status.json").read_text())
    assert status["state"] == "cancelled" and client_import.load_state(out["session_id"])["status"] == "failed"
    assert (case / "02_Original_Extraction" / RUN / "Manifest.db").exists(), "a collection that did not finish stays local"


# ---------------------------------------------------------------------------
# Helper hand-off (static)
# ---------------------------------------------------------------------------

def test_helper_pins_the_destination_and_keeps_the_token_out_of_the_payload():
    text = HELPER.read_text(encoding="utf-8")
    assert 'HelperVersion = "5.66"' in text
    run = text[text.index("void WriteNativeUsbRun("):text.index("void WriteNativePreview(")]
    assert 'StreamProblem(streamSession, streamToken, JsonStr(body, "stream_service"), out streamServer)' in run
    # The server comes from client-kit.json only, never from the request.
    assert 'JsonStr(body, "stream_server")' not in run and 'JsonStr(body, "server")' not in run
    assert 'server = KitServer();' in text and 'Aetheris\\client-kit.json' in text
    # Token replaced before the payload file is written.
    assert run.index('body = body.Replace("\\"" + streamToken + "\\"", "\\"\\"");') < run.index("File.WriteAllText(payloadFile, body")
    assert '"Local\\\\PolaronStreamUpload_" + stamp' in text
    assert 'path == "/acquisition/import/discard") Discard(ctx, caseDir);' in text
    discard = text[text.index("static void Discard("):text.index("static void Abort(")]
    assert "(a & ATTR_READONLY) != 0 || Sha256Of(full) != sha" in discard


def test_uploader_is_ascii_and_never_follows_junctions_when_deleting():
    raw = UPLOADER.read_bytes()
    assert all(b < 128 for b in raw), "Windows PowerShell 5.1 reads scripts without BOM as ANSI"
    text = raw.decode()
    remove = text[text.index("function Remove-Tree("):text.index("function Get-OwnPayloadTargets")]
    assert "ReparsePoint" in remove and "[IO.Directory]::Delete($Path)" in remove
    assert "Remove-Item" not in remove
    assert "Aetheris_Mobile_Payloads" in text and '$target.Contains("\\" + $stamp + "\\")' in text
