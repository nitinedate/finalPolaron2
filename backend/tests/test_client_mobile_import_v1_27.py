"""V1.27.0 — phone on a client PC: image created in C:\\evidence there, copied to the server.

Covers the server half without Windows or Docker:
* run selection from a Client Evidence Agent manifest (case folder or cases root share),
* the copy protocol against an in-process stand-in for the server HostDrive helper
  (/acquisition/import/prepare|chunk|commit|seal|abort, same rules as the C# writer),
* SHA-256 verification on the server side, no-overwrite, resume, cancel, platform guard,
* recovery/analysis paths pointing at the server copy, and the HTTP endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SHARE = "ce00112233445566778899aa"
TOKEN = "t" * 43
RUN = "CASE-1_E01_SM-A515F_FFS_20261115_083000"
OLD_RUN = "CASE-1_E01_SM-A515F_LOGICAL_20261101_101500"
CLIENT_CASE = "C:\\evidence\\CASE-1"
SERVER_CASES = "D:\\Polaron\\evidence\\cases"
RUN_FOLDERS = {"02_Original_Extraction", "03_Working_Copy", "05_Exports", "06_Reports", "07_Logs", "08_Hashes"}


# ---------------------------------------------------------------------------
# Stand-in for the server HostDrive helper's import writer (host-drive-helper.ps1)
# ---------------------------------------------------------------------------

class FakeHelper:
    def __init__(self, cases_root: Path):
        self.cases_root = cases_root
        self.corrupt_once: set[str] = set()
        self.commit_calls: dict[str, int] = {}
        self.sealed: list[str] = []
        self.chunk_delay = 0.0
        self.requests: list[str] = []
        helper = self

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

            def _rel(self):
                raw = self.headers.get("X-Import-Rel") or ""
                rel = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
                parts = rel.split("/")
                if len(parts) < 3 or parts[0] not in RUN_FOLDERS or not all(safe(p) for p in parts):
                    raise ValueError(f"bad rel {rel}")
                return rel

            def do_POST(self):
                helper.requests.append(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Origin"):
                    return self._send(403, {"ok": False, "error": "browser_forbidden"})
                if (self.headers.get("X-Import-Case-Root") or "") not in ("", "/evidence/cases"):
                    return self._send(400, {"ok": False, "error": "bad_request", "detail": "case_root"})
                case_id = self.headers.get("X-Import-Case-Id") or ""
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", case_id):
                    return self._send(400, {"ok": False, "error": "bad_request", "detail": "case id"})
                case_dir = helper.cases_root / case_id
                win_case = SERVER_CASES + "\\" + case_id
                try:
                    if self.path == "/acquisition/import/prepare":
                        existing, conflicts = [], []
                        for line in body.decode().splitlines():
                            size, rel = line.split("\t", 1)
                            target = case_dir / rel
                            if target.is_file():
                                (existing if target.stat().st_size == int(size) else conflicts).append(
                                    rel if target.stat().st_size == int(size) else {"rel": rel, "size": target.stat().st_size})
                        for sub in ("01_Authority", "02_Original_Extraction", "03_Working_Copy", "04_PA_Case", "05_Exports",
                                    "06_Reports", "07_Logs", "08_Hashes", "09_Review", "10_Disclosure"):
                            (case_dir / sub).mkdir(parents=True, exist_ok=True)
                        return self._send(200, {"ok": True, "case_dir": win_case, "existing": existing, "conflicts": conflicts})
                    if self.path == "/acquisition/import/chunk":
                        if helper.chunk_delay:
                            time.sleep(helper.chunk_delay)
                        rel = self._rel()
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
                        helper.commit_calls[rel] = helper.commit_calls.get(rel, 0) + 1
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
                        if rel in helper.corrupt_once:
                            helper.corrupt_once.discard(rel)
                            data = bytearray(part.read_bytes())
                            data[0] ^= 0xFF
                            part.write_bytes(bytes(data))
                        if part.stat().st_size != size:
                            part.unlink()
                            return self._send(422, {"ok": False, "error": "size_mismatch"})
                        sha = hashlib.sha256(part.read_bytes()).hexdigest()
                        if sha != expected:
                            part.unlink()
                            return self._send(422, {"ok": False, "error": "hash_mismatch", "sha256": sha})
                        mtime = int(self.headers.get("X-Import-Mtime-Ms") or 0)
                        if mtime:
                            os.utime(part, (mtime / 1000, mtime / 1000))
                        part.rename(final)
                        return self._send(200, {"ok": True, "exists": False, "sha256": sha, "path": win_case + "\\" + rel.replace("/", "\\")})
                    if self.path == "/acquisition/import/seal":
                        rels = [r for r in body.decode().splitlines() if r.startswith("02_Original_Extraction/")]
                        for rel in rels:
                            (case_dir / rel).chmod(0o444)
                        helper.sealed.extend(rels)
                        return self._send(200, {"ok": True, "sealed": len(rels), "failed": 0})
                    if self.path == "/acquisition/import/abort":
                        removed = 0
                        for rel in body.decode().splitlines():
                            part = Path(str(case_dir / rel) + ".polaron-part")
                            if part.exists():
                                part.unlink()
                                removed += 1
                        return self._send(200, {"ok": True, "removed": removed})
                except ValueError as exc:
                    return self._send(400, {"ok": False, "error": "bad_request", "detail": str(exc)})
                return self._send(404, {"ok": False, "error": "not_found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


# ---------------------------------------------------------------------------
# Client case folder as the HostDrive helper on the client PC leaves it
# ---------------------------------------------------------------------------

def _summary(run: str, os_family: str = "android") -> dict:
    j = lambda *p: "\\".join((CLIENT_CASE,) + p)  # noqa: E731
    return {
        "ok": True,
        "run_name": run,
        "file_count": 3,
        "total_bytes": 4096,
        "paths": {
            "case_root": CLIENT_CASE, "run_name": run, "original": j("02_Original_Extraction", run),
            "logs": j("07_Logs", run), "exports": j("05_Exports", run), "hashes": j("08_Hashes", run),
        },
        "device": {"os_family": os_family, "model": "SM-A515F", "serial": "R58N123"},
        "export_packages": {"ok": True, "packages": {"zip": j("05_Exports", run, f"{run}.zip")}},
        "acquisition_record": {"output_size": 4096, "file_count": 3},
        "evidence_package": {"run_name": run, "collection_summary": j("07_Logs", run, "collection_summary.json")},
    }


def build_client_case(root: Path, *, os_family: str = "android") -> dict[str, bytes]:
    files = {
        f"02_Original_Extraction/{RUN}/sdcard/DCIM/Camera/IMG_0001.jpg": os.urandom(2 * 1024 * 1024 + 123_457),
        f"02_Original_Extraction/{RUN}/sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt14": os.urandom(5000),
        f"02_Original_Extraction/{RUN}/sdcard/Documents/Notizen über München.txt": "Grüße".encode(),
        f"02_Original_Extraction/{RUN}/empty.txt": b"",
        f"03_Working_Copy/{RUN}/readable/contacts.csv": b"name,number\nA,1\n",
        f"05_Exports/{RUN}/{RUN}.zip": os.urandom(70_000),
        f"07_Logs/{RUN}/collection_summary.json": json.dumps(_summary(RUN, os_family)).encode(),
        f"07_Logs/{RUN}/collection.log": b"collected\n",
        f"08_Hashes/{RUN}/hashes.json": b"{}",
        "01_Authority/warrant.pdf": b"%PDF-authority",
        f"02_Original_Extraction/{OLD_RUN}/old.txt": b"old",
        f"07_Logs/{OLD_RUN}/collection_summary.json": json.dumps(_summary(OLD_RUN, os_family)).encode(),
    }
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    old = time.time() - 14 * 86400
    for rel in files:
        if OLD_RUN in rel:
            os.utime(root / rel, (old, old))
    return files


def manifest_for(root: Path, share_id: str = SHARE, client_root: str = CLIENT_CASE) -> dict:
    from app.services.client_evidence.agent_client import manifest_digest

    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            files.append({"path": p.relative_to(root).as_posix(), "size": p.stat().st_size,
                          "mtime_ms": int(p.stat().st_mtime * 1000)})
    files.sort(key=lambda f: f["path"])
    return {"ok": True, "share_id": share_id, "root": client_root, "hostname": "EXAMINER-PC-07",
            "files": files, "manifest_sha256": manifest_digest(files), "truncated": False, "agent_version": "1.2.0"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    from app.services.client_evidence import registry

    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(registry, "_redis", lambda: server)
    return server


@pytest.fixture
def env(tmp_path, monkeypatch, fake_redis):
    from app.services.client_evidence import agent_client, job_source, mount
    from app.services.mobile_acquire import host_bridge, run_registry

    client = tmp_path / "client" / "CASE-1"
    files = build_client_case(client)
    server_cases = tmp_path / "server" / "evidence" / "cases"
    server_cases.mkdir(parents=True)
    helper = FakeHelper(server_cases)
    released: list[str] = []
    health = {"ok": True, "client_import": True, "computer_name": "FORENSIC-SRV", "helper_version": "5.58",
              "import_case_root": SERVER_CASES, "root": "D:\\Polaron"}

    monkeypatch.setenv("CLIENT_MOBILE_IMPORT_CHUNK_MB", "1")
    monkeypatch.setattr(agent_client, "_container_networks", lambda: [])
    monkeypatch.setattr(agent_client, "choose_reachable",
                        lambda candidates, **kw: (candidates[0], manifest_for(client)))
    monkeypatch.setattr(mount, "ensure_mounted", lambda share_id, **kw: client)
    monkeypatch.setattr(job_source, "release_share", lambda share_id, **kw: released.append(share_id))
    monkeypatch.setattr(host_bridge, "helper_reachable", lambda *a, **kw: True)
    monkeypatch.setattr(host_bridge, "helper_health", lambda: dict(health))
    monkeypatch.setattr(host_bridge, "helper_base_url", lambda: helper.url)
    monkeypatch.setattr(run_registry, "_HELPER_ROOTS_CACHE", (0.0, []))
    yield {"client": client, "files": files, "server": server_cases, "helper": helper, "released": released,
           "health": health, "redis": fake_redis}
    helper.close()


def start(env, **overrides):
    from app.services.mobile_acquire import client_import

    m = manifest_for(env["client"])
    kwargs = dict(
        share_id=SHARE, token=TOKEN, port=9877, addresses=["10.20.30.41"], hostname="EXAMINER-PC-07",
        client_root=CLIENT_CASE, file_count=len(m["files"]), total_bytes=sum(f["size"] for f in m["files"]),
        manifest_sha256=m["manifest_sha256"], agent_url=None, relay_supported=False, skipped=0,
        agent_version="1.2.0", case_id="CASE-1", run_name="", scope="full", request_ip="10.20.30.41",
        user_email="examiner@lab", platform="android",
    )
    kwargs.update(overrides)
    return client_import.start_import(**kwargs)


def wait(import_id: str, timeout: float = 30.0) -> dict:
    from app.services.mobile_acquire import client_import

    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client_import.load_state(import_id)
        if state and state["status"] in client_import.TERMINAL:
            return state
        time.sleep(0.05)
    raise AssertionError("import did not finish")


# ---------------------------------------------------------------------------
# Run selection
# ---------------------------------------------------------------------------

def test_select_run_picks_newest_run_and_only_its_run_folders(tmp_path):
    from app.services.mobile_acquire.client_import import select_run

    client = tmp_path / "CASE-1"
    build_client_case(client)
    files = manifest_for(client)["files"]
    sel = select_run(files, client_root=CLIENT_CASE)
    assert sel.run_name == RUN and sel.case_prefix == "" and sel.case_folder == "CASE-1" and sel.has_summary
    rels = {f.rel for f in sel.files}
    assert f"05_Exports/{RUN}/{RUN}.zip" in rels and f"03_Working_Copy/{RUN}/readable/contacts.csv" in rels
    assert not any(r.startswith("01_Authority") or OLD_RUN in r for r in rels)
    assert sel.files[0].rel.startswith("07_Logs/"), "logs travel first so a partial copy is self-describing"

    packages = select_run(files, client_root=CLIENT_CASE, scope="packages")
    assert {f.rel.split("/")[0] for f in packages.files} == {"05_Exports", "07_Logs", "08_Hashes"}

    older = select_run(files, client_root=CLIENT_CASE, run_name=OLD_RUN.lower())
    assert older.run_name == OLD_RUN


def test_select_run_from_the_cases_root_needs_the_case(tmp_path):
    from app.services.client_evidence import ClientEvidenceError
    from app.services.mobile_acquire.client_import import select_run

    root = tmp_path / "evidence"
    build_client_case(root / "CASE-1")
    build_client_case(root / "CASE_2")
    files = manifest_for(root, client_root="C:\\evidence")["files"]
    with pytest.raises(ClientEvidenceError, match="several cases"):
        select_run(files, client_root="C:\\evidence")
    sel = select_run(files, client_root="C:\\evidence", case_id="case 2")
    assert sel.case_prefix == "CASE_2" and sel.case_folder == "CASE_2"
    assert all(f.share_rel.startswith("CASE_2/") and not f.rel.startswith("CASE_2") for f in sel.files)
    with pytest.raises(ClientEvidenceError, match="not found"):
        select_run(files, client_root="C:\\evidence", case_id="CASE-9")


def test_select_run_refuses_empty_folders_unknown_runs_and_unstorable_names():
    from app.services.client_evidence import ClientEvidenceError
    from app.services.mobile_acquire.client_import import select_run

    with pytest.raises(ClientEvidenceError, match="No acquisition run"):
        select_run([{"path": "random/file.bin", "size": 1, "mtime_ms": 1}], client_root=CLIENT_CASE)
    base = [{"path": f"07_Logs/{RUN}/collection_summary.json", "size": 2, "mtime_ms": 1}]
    with pytest.raises(ClientEvidenceError, match="was not found"):
        select_run(base, client_root=CLIENT_CASE, run_name="NOPE")
    bad = base + [{"path": f"02_Original_Extraction/{RUN}/a:b.txt", "size": 1, "mtime_ms": 1}]
    with pytest.raises(ClientEvidenceError, match="cannot store"):
        select_run(bad, client_root=CLIENT_CASE)


def test_rebase_paths_and_server_names():
    from app.services.mobile_acquire.client_import import rebase_paths, server_case_folder, server_paths

    data = {"paths": {"exports": "C:\\evidence\\CASE-1\\05_Exports\\R", "other": "C:\\evidence\\CASE-10\\x"},
            "list": ["c:/evidence/case-1/07_Logs/R/collection_summary.json"], "n": 3}
    out = rebase_paths(data, CLIENT_CASE, "D:\\srv\\cases\\CASE-1")
    assert out["paths"]["exports"] == "D:\\srv\\cases\\CASE-1\\05_Exports\\R"
    assert out["paths"]["other"] == "C:\\evidence\\CASE-10\\x", "a sibling case with a longer name is not rebased"
    assert out["list"][0] == "D:\\srv\\cases\\CASE-1\\07_Logs\\R\\collection_summary.json" and out["n"] == 3
    assert server_case_folder("CASE-1") == "CASE-1"
    assert server_case_folder("केस 12") == "CASE_12" or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", server_case_folder("केस 12"))
    assert server_paths("D:\\c\\X", "R", "packages")["original"] == ""
    assert server_paths("D:\\c\\X", "R", "full")["original"] == "D:\\c\\X\\02_Original_Extraction\\R"


# ---------------------------------------------------------------------------
# End to end: client share -> server helper
# ---------------------------------------------------------------------------

def test_copy_is_byte_identical_verified_sealed_and_documented(env):
    from app.services.mobile_acquire import client_import

    started = start(env)
    assert started["status"] == "queued" and started["run_name"] == RUN and started["client_host"] == "EXAMINER-PC-07"
    state = wait(started["import_id"])
    assert state["status"] == "completed", state.get("error")

    case_dir = env["server"] / "CASE-1"
    copied = [r for r in env["files"] if r.split("/")[1:2] == [RUN] and not r.startswith("01_")]
    for rel in copied:
        target = case_dir / rel
        assert target.read_bytes() == env["files"][rel], rel
        assert abs(target.stat().st_mtime - (env["client"] / rel).stat().st_mtime) < 0.01, "modified time preserved"
    assert not (case_dir / "01_Authority" / "warrant.pdf").exists()
    assert not (case_dir / "02_Original_Extraction" / OLD_RUN).exists()
    assert not list(case_dir.rglob("*.polaron-part"))
    assert sorted(env["helper"].sealed) == sorted(r for r in copied if r.startswith("02_"))
    assert env["released"] == [SHARE], "the share is revoked on the PC once the server copy exists"

    # Provenance and hash list next to the run, with every copied file's SHA-256.
    iid = started["import_id"]
    record = json.loads((case_dir / "07_Logs" / RUN / f"client_import_{iid}.json").read_text())
    assert record["schema"] == client_import.SCHEMA and record["client"]["hostname"] == "EXAMINER-PC-07"
    assert record["client"]["case_dir"] == CLIENT_CASE and record["server"]["case_dir"] == SERVER_CASES + "\\CASE-1"
    hash_list = (case_dir / record["hash_list"]).read_text().splitlines()
    listed = dict(reversed(line.split("  ", 1)) for line in hash_list)
    for rel in copied:
        assert listed[rel] == hashlib.sha256(env["files"][rel]).hexdigest()
    assert record["hash_list_sha256"] == hashlib.sha256((case_dir / record["hash_list"]).read_bytes()).hexdigest()

    # The result the wizard receives points at the server copy, ready for analysis.
    result = state["result"]
    server_case = SERVER_CASES + "\\CASE-1"
    assert result["ok"] and result["stage_reached"] == "complete" and result["run_name"] == RUN
    assert result["paths"]["exports"] == f"{server_case}\\05_Exports\\{RUN}"
    assert result["paths"]["original"] == f"{server_case}\\02_Original_Extraction\\{RUN}"
    assert result["export_packages"]["packages"]["zip"] == f"{server_case}\\05_Exports\\{RUN}\\{RUN}.zip"
    assert not json.dumps(result).count("C:\\\\evidence\\\\CASE-1\\\\0"), "no client paths left in the result"
    assert result["client_import"]["server_case_dir"] == server_case
    assert state["run"]["status"] == "completed" and state["run"]["result"]["paths"] == result["paths"]
    assert state["bytes_done"] == state["total_bytes"] and state["files_done"] == state["total_files"]

    # The client's own summary was copied unchanged (byte-identical evidence log).
    assert (case_dir / "07_Logs" / RUN / "collection_summary.json").read_bytes() == env["files"][f"07_Logs/{RUN}/collection_summary.json"]


def test_recovery_after_an_api_restart_uses_the_server_copy(env):
    from app.services.mobile_acquire import run_registry

    state = wait(start(env)["import_id"])
    assert state["status"] == "completed"
    server_case = SERVER_CASES + "\\CASE-1"
    full = run_registry._recover_summary_from_disk(str(env["server"]), "CASE-1")
    assert full["paths"]["exports"] == f"{server_case}\\05_Exports\\{RUN}"
    assert full["paths"]["case_root"] == server_case
    runs = run_registry.get_registry().recover(str(env["server"]), "CASE-1")
    mine = [r for r in runs if r.get("run_name") == RUN]
    assert mine and mine[0]["result"]["paths"]["exports"] == f"{server_case}\\05_Exports\\{RUN}"
    assert any("copied to the forensic server" in w for w in mine[0]["result"]["warnings"])


def test_bytes_corrupted_on_the_server_disk_are_caught_and_sent_again(env):
    rel = f"02_Original_Extraction/{RUN}/sdcard/DCIM/Camera/IMG_0001.jpg"
    env["helper"].corrupt_once.add(rel)
    state = wait(start(env)["import_id"])
    assert state["status"] == "completed", state.get("error")
    assert env["helper"].commit_calls[rel] == 2
    assert (env["server"] / "CASE-1" / rel).read_bytes() == env["files"][rel]


def test_files_already_verified_on_the_server_are_kept_and_different_ones_refused(env):
    case_dir = env["server"] / "CASE-1"
    keep = f"05_Exports/{RUN}/{RUN}.zip"
    (case_dir / keep).parent.mkdir(parents=True)
    (case_dir / keep).write_bytes(env["files"][keep])
    state = wait(start(env)["import_id"])
    assert state["status"] == "completed"
    assert state["files_present"] == 1 and any("already on the forensic server" in w for w in state["warnings"])

    other = env["server"].parent / "other" / "evidence" / "cases"
    other.mkdir(parents=True)
    env["helper"].cases_root = other
    clash = other / "CASE-1" / keep
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"something else entirely")
    failed = wait(start(env)["import_id"])
    assert failed["status"] == "failed" and "different content" in failed["error"]
    assert clash.read_bytes() == b"something else entirely", "evidence on the server is never overwritten"
    assert env["released"][-1] == SHARE


def test_cancel_stops_the_copy_and_leaves_no_unverified_parts(env):
    from app.services.mobile_acquire import client_import

    env["helper"].chunk_delay = 0.2
    started = start(env)
    time.sleep(0.5)
    assert client_import.request_cancel(started["import_id"]) is True
    state = wait(started["import_id"])
    assert state["status"] == "cancelled"
    assert not list((env["server"] / "CASE-1").rglob("*.polaron-part"))
    assert client_import.request_cancel(started["import_id"]) is False
    assert env["released"] == [SHARE]


def test_wrong_platform_backend_refuses_the_copy(env):
    state = wait(start(env, platform="ios")["import_id"])
    assert state["status"] == "failed" and "android device" in state["error"]


def test_old_or_missing_server_helper_is_reported_before_anything_is_shared(env):
    from app.services.client_evidence import ClientEvidenceError, registry

    env["health"]["client_import"] = False
    env["health"]["helper_version"] = "5.57"
    with pytest.raises(ClientEvidenceError, match="5.58"):
        start(env)
    assert registry.load_share(SHARE) is None


def test_truncated_share_is_refused(env, monkeypatch):
    from app.services.client_evidence import ClientEvidenceError, agent_client, registry

    m = manifest_for(env["client"])
    m["truncated"] = True
    monkeypatch.setattr(agent_client, "choose_reachable", lambda c, **kw: (c[0], m))
    with pytest.raises(ClientEvidenceError, match="complete copy is not possible"):
        start(env)
    assert registry.load_share(SHARE) is None


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import acquisition as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    user = type("U", (), {"email": "examiner@lab", "id": "u1"})()
    for route in router_mod.router.routes:
        for dep in route.dependant.dependencies:
            app.dependency_overrides[dep.call] = lambda: user
    monkeypatch.setattr(router_mod, "_required_mobile_platform", lambda: "android")
    return TestClient(app)


BODY = {"share_id": SHARE, "token": TOKEN, "port": 9877, "addresses": ["10.20.30.41"], "hostname": "PC-7",
        "client_root": CLIENT_CASE, "file_count": 3, "total_bytes": 10, "manifest_sha256": "a" * 64,
        "case_id": "CASE-1", "scope": "full"}


def test_endpoints_start_poll_cancel_and_map_errors(api_client, monkeypatch):
    from app.services.client_evidence import ClientEvidenceError
    from app.services.mobile_acquire import client_import

    seen = {}

    def fake_start(**kw):
        seen.update(kw)
        return {"import_id": "a" * 20, "status": "queued"}

    monkeypatch.setattr(client_import, "start_import", fake_start)
    r = api_client.post("/api/acquisition/client-import", json=BODY, headers={"X-Forwarded-For": "10.20.30.41"})
    assert r.status_code == 200 and r.json()["import_id"] == "a" * 20
    assert seen["request_ip"] == "10.20.30.41" and seen["platform"] == "android" and seen["user_email"] == "examiner@lab"

    for message, status in [
        ("The forensic server could not reach the Client Evidence Agent", 502),
        ("The HostDrive helper on the forensic server is not running", 503),
        ("Run X has no files to copy.", 400),
    ]:
        monkeypatch.setattr(client_import, "start_import", lambda _m=message, **kw: (_ for _ in ()).throw(ClientEvidenceError(_m)))
        r = api_client.post("/api/acquisition/client-import", json=BODY)
        assert r.status_code == status and r.json()["detail"]["error"]["message"] == message

    assert api_client.post("/api/acquisition/client-import", json={**BODY, "share_id": "../x"}).status_code == 422
    assert api_client.post("/api/acquisition/client-import", json={**BODY, "scope": "everything"}).status_code == 422

    monkeypatch.setattr(client_import, "load_state", lambda iid: None)
    assert api_client.get("/api/acquisition/client-import/" + "b" * 20).status_code == 404
    monkeypatch.setattr(client_import, "load_state", lambda iid: {"import_id": iid, "status": "running", "total_bytes": 200, "bytes_done": 50})
    body = api_client.get("/api/acquisition/client-import/" + "b" * 20).json()
    assert body["progress_pct"] == 25.0
    monkeypatch.setattr(client_import, "request_cancel", lambda iid: True)
    assert api_client.post("/api/acquisition/client-import/" + "b" * 20 + "/cancel").json()["accepted"] is True


def test_capabilities_report_the_server_helper_identity(api_client, monkeypatch):
    from app.services.mobile_acquire import client_import

    monkeypatch.setattr(client_import, "helper_capabilities",
                        lambda: {"reachable": True, "client_import": True, "computer_name": "FORENSIC-SRV", "helper_version": "5.58"})
    body = api_client.get("/api/acquisition/client-import/capabilities").json()
    assert body["office_helper"]["computer_name"] == "FORENSIC-SRV"
    assert body["default_client_case_root"] == "C:\\evidence" and body["server_case_root"] == "/evidence/cases"


# ---------------------------------------------------------------------------
# Shipped helper script
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def test_helper_script_ships_the_import_writer_and_identity():
    text = (ROOT / "scripts" / "host-drive-helper.ps1").read_text(encoding="utf-8-sig")
    version = re.search(r'\$script:HelperVersion = "(\d+)\.(\d+)"', text)
    assert version and (int(version.group(1)), int(version.group(2))) >= (5, 58)
    assert "public static class AetherisImport" in text
    for endpoint in ("prepare", "chunk", "commit", "seal", "abort"):
        assert f'"/acquisition/import/{endpoint}"' in text
    assert 'path.StartsWith("/acquisition/import/", StringComparison.Ordinal)' in text
    assert "ThreadPool.QueueUserWorkItem(delegate(object state) { AetherisImport.Handle(importCtx, importPath); });" in text
    assert text.count("computer_name") >= 2 and text.count("client_import") >= 2
    assert "browser_forbidden" in text and "MOVEFILE_WRITE_THROUGH" in text
    ensure = (ROOT / "scripts" / "ensure-host-drive-helper.ps1").read_text(encoding="utf-8-sig")
    assert f'$ExpectedHelperVersion = "{version.group(1)}.{version.group(2)}"' in ensure
    # The C# block lives in an expandable here-string: a stray '$' or backtick in the new
    # class would be rewritten by PowerShell before compilation.
    cls = text[text.index("public static class AetherisImport"):text.index('"@', text.index("public static class AetherisImport"))]
    assert "$" not in cls and "`" not in cls
