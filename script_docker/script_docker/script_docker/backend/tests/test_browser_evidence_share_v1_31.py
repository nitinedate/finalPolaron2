"""V1.31: "This computer" evidence served by the examiner's browser tab (no agent to install).

Unit tests cover the server side of a browser-served folder (outbound connection only, the PC is
never dialed). The end-to-end test runs the real TypeScript tab code (frontend/src/lib/
browserEvidenceShare.ts, bundled for Node by the test runner) against the real relay endpoints
and Redis, then reads the evidence back with the server's own FUSE block reader.

End-to-end prerequisites (skipped otherwise):
  CLIENT_EVIDENCE_TEST_REDIS_URL  reachable Redis, e.g. redis://127.0.0.1:6390/0
  BROWSER_SHARE_E2E_DRIVER        path to the Node driver that loads the bundled module
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

JOB = "11111111-2222-3333-4444-555555555555"


class _FakeDB:
    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    from app.services.client_evidence import registry

    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(registry, "_redis", lambda: server)
    monkeypatch.setenv("CLIENT_EVIDENCE_SECRET", "unit-test-secret")
    return server


@pytest.fixture
def job_env(monkeypatch):
    from app.services.client_evidence import job_source, mount

    state = {"status": "created", "ds": {}}
    logs: list[str] = []
    monkeypatch.setattr(job_source, "_load_ds", lambda db, job_id: (json.loads(json.dumps(state["ds"])), state["status"]))
    monkeypatch.setattr(job_source, "_save_ds", lambda db, job_id, ds: state.__setitem__("ds", ds))
    monkeypatch.setattr("app.services.disk_build_log.write_disk_log", lambda db, job_id, msg, **kw: logs.append(msg))
    monkeypatch.setattr(mount, "ensure_mounted", lambda sid, **kw: Path("/client-evidence") / sid)
    monkeypatch.setattr(mount, "unmount", lambda sid: None)
    return {"state": state, "logs": logs}


def _manifest(share_id: str, files: list[dict]) -> dict:
    from app.services.client_evidence.agent_client import manifest_digest

    return {"ok": True, "agent_version": "browser-1.0.0", "share_id": share_id, "root": "Case-0423",
            "hostname": "This computer (browser on Windows)", "files": files, "dirs": [],
            "manifest_sha256": manifest_digest(files)}


def test_browser_folder_uses_only_the_outbound_connection_and_is_recorded(fake_redis, job_env, monkeypatch):
    from app.services.client_evidence import agent_client, registry, relay
    from app.services.client_evidence.job_source import attach_client_source, client_source_status

    share = "ce" + "b" * 24
    files = [{"path": "laptop.E01", "size": 10, "mtime_ms": 1_700_000_000_000}]
    dialed: list[list[str]] = []
    monkeypatch.setattr(agent_client, "build_candidates", lambda **kw: (_ for _ in ()).throw(AssertionError("PC dialed")))
    real_choose = agent_client.choose_reachable

    def choose(candidates, **kw):
        dialed.append(list(candidates))
        return real_choose(candidates, **kw)

    monkeypatch.setattr(agent_client, "choose_reachable", choose)
    monkeypatch.setattr(relay, "get_manifest", lambda sid, **kw: _manifest(sid, files))
    out = attach_client_source(
        _FakeDB(), JOB, share_id=share, token="t" * 43, port=9877, addresses=[], hostname="This computer (browser on Windows)",
        client_root="Case-0423", file_count=1, total_bytes=10, manifest_sha256=_manifest(share, files)["manifest_sha256"],
        source_type="mobile", agent_url=None, request_ip="192.168.1.50", relay_supported=True, browser_share=True,
    )
    assert dialed == [[relay.RELAY_URL]]  # the LAN request address was never tried
    assert out["transport"] == "relay" and out["browser_share"] is True
    assert registry.load_share(share).agent_url == relay.RELAY_URL
    cs = job_env["state"]["ds"]["client_source"]
    assert cs["browser_share"] is True and cs["agent_version"] == "browser-1.0.0"
    assert "browser tab" in job_env["logs"][-1]
    monkeypatch.setattr(relay, "agent_online", lambda sid: True)
    status = client_source_status(_FakeDB(), JOB, probe=True)
    assert status["browser_share"] is True and status["transport"] == "relay" and status["agent_online"] is True


def test_browser_folder_needs_the_outbound_connection(fake_redis, job_env, monkeypatch):
    from app.services.client_evidence import ClientEvidenceError
    from app.services.client_evidence.job_source import attach_client_source

    monkeypatch.setenv("CLIENT_EVIDENCE_RELAY_ENABLED", "false")
    with pytest.raises(ClientEvidenceError, match="CLIENT_EVIDENCE_RELAY_ENABLED"):
        attach_client_source(
            _FakeDB(), JOB, share_id="ce" + "c" * 24, token="t" * 43, port=9877, addresses=[], hostname="",
            client_root="Case", file_count=1, total_bytes=1, manifest_sha256="", source_type="disk", agent_url=None,
            request_ip=None, relay_supported=True, browser_share=True,
        )


def test_request_schema_accepts_the_browser_flag():
    from app.schemas.forensic import ClientSourceBody

    body = ClientSourceBody(share_id="ce" + "d" * 24, token="t" * 43, browser=True, relay=True)
    assert body.browser is True and body.addresses == [] and body.port == 9877
    assert ClientSourceBody(share_id="ce" + "d" * 24, token="t" * 43).browser is False


# ---------------------------------------------------------------------------
# End to end: the real TypeScript tab code through the real relay endpoints
# ---------------------------------------------------------------------------

def _redis_url() -> str | None:
    url = os.environ.get("CLIENT_EVIDENCE_TEST_REDIS_URL")
    if not url:
        return None
    try:
        import redis

        redis.Redis.from_url(url, socket_connect_timeout=2).ping()
        return url
    except Exception:
        return None


@pytest.fixture
def relay_server(monkeypatch):
    url = _redis_url()
    driver = os.environ.get("BROWSER_SHARE_E2E_DRIVER")
    if not url or not driver or not Path(driver).is_file() or not shutil.which("node"):
        pytest.skip("needs CLIENT_EVIDENCE_TEST_REDIS_URL, BROWSER_SHARE_E2E_DRIVER and node")
    uvicorn = pytest.importorskip("uvicorn")
    from fastapi import FastAPI

    from app.routers import client_evidence as router_mod
    from app.services.client_evidence import registry, relay

    monkeypatch.setenv("REDIS_URL", url)
    monkeypatch.setenv("CLIENT_EVIDENCE_SECRET", "unit-test-secret")
    monkeypatch.setattr(relay, "_sync_client", None)
    monkeypatch.setattr(relay, "_async_clients", {})
    import redis

    client = redis.Redis.from_url(url, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr(registry, "_redis", lambda: client)
    app = FastAPI()
    app.include_router(router_mod.agent_router)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield {"base": f"http://127.0.0.1:{port}", "driver": driver}
    server.should_exit = True
    thread.join(timeout=10)


def _evidence_tree(root: Path) -> dict[str, bytes]:
    blobs = {
        "laptop.E01": os.urandom(3 * 1024 * 1024 + 123),            # incompressible, like an E01
        "laptop.E02": os.urandom(700_001),
        "sub/zeros.dd": bytes(2 * 1024 * 1024) + os.urandom(4096),  # empty space: "zero" blocks
        "sub/system.log": b"2026-11-16 06:43:00 INFO service started ok\n" * 40_000,  # compresses well
        "sub/empty.txt": b"",
        "sub/Café résumé.txt": "non-ASCII name".encode(),
        "A-upper.txt": b"ordinal sort puts upper case first",
    }
    for rel, data in blobs.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "$RECYCLE.BIN").mkdir()
    (root / "$RECYCLE.BIN" / "deleted.bin").write_bytes(b"left out like the agent does")
    return blobs


def test_browser_tab_serves_the_folder_byte_for_byte_through_the_relay(relay_server, job_env, tmp_path, monkeypatch):
    from app.services.client_evidence import agent_client, relay
    from app.services.client_evidence.fusefs import EvidenceReadError, RemoteReader
    from app.services.client_evidence.job_source import attach_client_source

    root = tmp_path / "Case-0423"
    root.mkdir()
    blobs = _evidence_tree(root)
    proc = subprocess.Popen(
        ["node", relay_server["driver"], f"{relay_server['base']}/api/client-agent/relay", str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = proc.stdout.readline()
        assert line, proc.stderr.read()
        tab = json.loads(line)
        # 1. Same frozen file list and digest the agent would compute (server-side formula).
        local = sorted(
            ({"path": rel, "size": len(data), "mtime_ms": int((root / rel).stat().st_mtime_ns // 1_000_000)}
             for rel, data in blobs.items()),
            key=lambda f: f["path"],
        )
        assert [f["path"] for f in local] == tab["paths"]  # $RECYCLE.BIN left out, ordinal order
        assert tab["manifest_sha256"] == agent_client.manifest_digest(local)
        assert tab["file_count"] == len(blobs) and tab["total_bytes"] == sum(len(d) for d in blobs.values())

        # 2. Registration exactly like the UI: browser share, outbound connection only.
        out = attach_client_source(
            _FakeDB(), JOB, share_id=tab["share_id"], token=tab["token"], port=9877, addresses=[],
            hostname=tab["hostname"], client_root=tab["root"], file_count=tab["file_count"],
            total_bytes=tab["total_bytes"], manifest_sha256=tab["manifest_sha256"], source_type="disk",
            agent_url=None, request_ip="192.168.1.50", relay_supported=True, browser_share=True,
        )
        assert out["transport"] == "relay" and out["file_count"] == len(blobs)
        assert relay.agent_online(tab["share_id"])

        # 3. The server's own block reader gets every byte back, with zero/deflate on the wire.
        files = [{"path": f["path"], "size": f["size"], "mtime_ms": f["mtime_ms"]} for f in local]
        reader = RemoteReader(share_id=tab["share_id"], token=tab["token"], base_urls=[relay.RELAY_URL], files=files,
                              block_size=128 * 1024, readahead_blocks=0, encoding="zero,deflate")
        for index, meta in enumerate(files):
            data = blobs[meta["path"]]
            got = reader.read(index, 0, len(data)) if data else b""
            assert hashlib.sha256(got).hexdigest() == hashlib.sha256(data).hexdigest(), meta["path"]
        e01 = next(i for i, f in enumerate(files) if f["path"] == "laptop.E01")
        for offset, size in ((1, 17), (131_071, 3), (2_999_999, 70_000), (3 * 1024 * 1024 + 100, 23)):
            assert reader.read(e01, offset, size) == blobs["laptop.E01"][offset:offset + size]
        # Empty space and text were compressed: far fewer bytes crossed the network than were read.
        assert reader.wire_bytes < reader.bytes_fetched - 3 * 1024 * 1024
        reader.close()
        # Scattered reads inside empty space travel as "zero" markers (nothing on the wire).
        zeros = next(i for i, f in enumerate(files) if f["path"] == "sub/zeros.dd")
        scattered = RemoteReader(share_id=tab["share_id"], token=tab["token"], base_urls=[relay.RELAY_URL], files=files,
                                 block_size=128 * 1024, readahead_blocks=0, encoding="zero,deflate")
        for offset in range(0, 1024 * 1024, 256 * 1024):
            assert scattered.read(zeros, offset, 4096) == bytes(4096)
        assert scattered.zero_bytes == 4 * 128 * 1024 and scattered.wire_bytes == 0
        scattered.close()

        # 4. A file changed after it was chosen stops processing (409), like the agent.
        changed = root / "laptop.E02"
        changed.write_bytes(os.urandom(700_001))
        os.utime(changed, (time.time() + 60, time.time() + 60))
        e02 = next(i for i, f in enumerate(files) if f["path"] == "laptop.E02")
        fresh = RemoteReader(share_id=tab["share_id"], token=tab["token"], base_urls=[relay.RELAY_URL], files=files,
                             block_size=128 * 1024, readahead_blocks=0, encoding="zero,deflate", retry_seconds=5)
        with pytest.raises(EvidenceReadError, match="changed on the client workstation"):
            fresh.read(e02, 0, 1000)
        fresh.close()

        # 5. Releasing the folder on the server stops this tab serving it.
        assert relay.revoke(tab["share_id"])
        deadline = time.time() + 15
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        assert proc.poll() == 0, "the tab kept serving a revoked folder"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
