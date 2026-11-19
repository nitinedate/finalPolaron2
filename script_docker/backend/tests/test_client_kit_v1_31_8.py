"""V1.31.8: the server hands each examiner computer its Polaron client files (helper, collection scripts)."""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers import client_kit

ROOT = Path(__file__).resolve().parents[2]
PWSH = shutil.which("pwsh") or str(Path.home() / ".local" / "pwsh" / "pwsh")


@pytest.fixture()
def kit_scripts(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    (scripts / "client-evidence-agent").mkdir(parents=True)
    (scripts / "host-drive-helper.ps1").write_text('$script:HelperVersion = "5.63"  # test\n', encoding="utf-8")
    (scripts / "native_ios_job.ps1").write_text("# job\n", encoding="utf-8")
    (scripts / "_debug_job_734.py").write_text("print('server diagnostic')\n", encoding="utf-8")
    (scripts / "purge.sql").write_text("DELETE FROM jobs;\n", encoding="utf-8")
    (scripts / "client-evidence-agent" / "polaron-client-evidence-agent.ps1").write_text("# agent\n", encoding="utf-8")
    shutil.copy(ROOT / "scripts" / "polaron-client-update.ps1", scripts / "polaron-client-update.ps1")
    monkeypatch.setenv("POLARON_CLIENT_KIT_SCRIPTS_DIR", str(scripts))
    return scripts


@pytest.fixture()
def client(kit_scripts):
    app = FastAPI()
    app.include_router(client_kit.router)
    return TestClient(app)


def _key_headers():
    return {client_kit.KIT_KEY_HEADER: client_kit.kit_key()}


def test_manifest_needs_the_kit_key(client):
    assert client.get("/api/client-kit/manifest").status_code == 401
    assert client.get("/api/client-kit/manifest", headers={client_kit.KIT_KEY_HEADER: "0" * 48}).status_code == 401
    assert client.get("/api/client-kit/manifest", headers=_key_headers()).status_code == 200


def test_manifest_lists_client_files_only(client, kit_scripts):
    data = client.get("/api/client-kit/manifest", headers=_key_headers()).json()
    paths = {f["path"]: f for f in data["files"]}
    assert data["helper_version"] == "5.63"
    assert "scripts/host-drive-helper.ps1" in paths
    assert "scripts/polaron-client-update.ps1" in paths
    assert "scripts/client-evidence-agent/polaron-client-evidence-agent.ps1" in paths
    assert "backend/app/routers/client_kit.py" in paths  # the Python that collections run on the client
    assert "scripts/_debug_job_734.py" not in paths and "scripts/purge.sql" not in paths
    assert not any(p.startswith(("frontend/", ".env")) or "/.env" in p or "__pycache__" in p for p in paths)
    expected = hashlib.sha256((kit_scripts / "host-drive-helper.ps1").read_bytes()).hexdigest()
    assert paths["scripts/host-drive-helper.ps1"]["sha256"] == expected
    assert len(data["kit_version"]) == 16


def test_file_serves_listed_paths_only(client, kit_scripts):
    r = client.get("/api/client-kit/file", params={"path": "scripts/host-drive-helper.ps1"}, headers=_key_headers())
    assert r.status_code == 200
    assert r.content == (kit_scripts / "host-drive-helper.ps1").read_bytes()
    assert r.headers["X-Polaron-Sha256"] == hashlib.sha256(r.content).hexdigest()
    for bad in ("../.env", "scripts/../../.env", "scripts/_debug_job_734.py", "scripts/purge.sql", ".env"):
        assert client.get("/api/client-kit/file", params={"path": bad}, headers=_key_headers()).status_code == 404
    assert client.get("/api/client-kit/file", params={"path": "scripts/host-drive-helper.ps1"}).status_code == 401


def test_kit_key_follows_the_server_secret(monkeypatch):
    first = client_kit.kit_key()
    settings = client_kit.get_settings()
    monkeypatch.setattr(settings, "jwt_secret", settings.jwt_secret + "-rotated")
    assert client_kit.kit_key() != first and len(first) == 48


def test_info_reports_the_server_helper_version(kit_scripts):
    assert client_kit.client_kit_info(current=None) == {
        "ok": True,
        "helper_version": "5.63",
        "setup_file": "Polaron-Setup-This-Computer.cmd",
    }


def test_setup_file_names_this_server_and_is_a_cmd(kit_scripts, monkeypatch):
    monkeypatch.setenv("AETHERIS_SERVICE", "mobile-android")
    r = client_kit.client_kit_setup(origin="https://forensic.example.in/", current=None)
    body = r.body.decode("ascii")
    assert r.headers["Content-Disposition"] == 'attachment; filename="Polaron-Setup-This-Computer.cmd"'
    assert body.startswith("@echo off\r\n") and "\n" not in body.replace("\r\n", "")
    assert "$Server = 'https://forensic.example.in'" in body
    assert "$Service = 'mobile-android'" in body
    assert f"$KitKey = '{client_kit.kit_key()}'" in body
    # The launcher must not find its own marker text before the PowerShell part.
    launcher, ps_part = body.split("\r\n#POLARON-PS-BEGIN\r\n", 1)
    assert "#POLARON-PS-BEGIN" not in launcher
    assert "-Setup" in ps_part and "Get-FileHash" in ps_part


@pytest.mark.parametrize(
    "origin",
    ["https://x.example/'; calc", 'https://x.example" & calc', "https://x.example/path", "file:///c:/x", "javascript:alert(1)"],
)
def test_setup_refuses_unsafe_origins(kit_scripts, origin):
    with pytest.raises(HTTPException) as err:
        client_kit.client_kit_setup(origin=origin, current=None)
    assert err.value.status_code == 422


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not Path(PWSH).exists(), reason="pwsh not installed")
def test_setup_file_installs_the_kit_end_to_end(kit_scripts, tmp_path):
    """Runs the PowerShell part of a generated setup file against the real endpoints (served by uvicorn)."""
    port = _free_port()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "backend"))
    app_src = tmp_path / "kit_app.py"
    app_src.write_text(
        "from fastapi import FastAPI\nfrom app.routers.client_kit import router\napp = FastAPI()\napp.include_router(router)\n",
        encoding="utf-8",
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "kit_app:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=tmp_path, env=env,
    )
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        text = client_kit.setup_cmd_text(f"http://127.0.0.1:{port}", "forensic", client_kit.kit_key(), "now")
        ps_part = text.split("\r\n#POLARON-PS-BEGIN\r\n", 1)[1]
        client_root = tmp_path / "client"
        # Same call, without restarting a helper or installing the agent (no Windows here).
        ps_part = ps_part.replace(
            "-Setup\r\n",
            f"-Setup -NoHelperRestart -NoAgentInstall -Root '{client_root}' -ConfigFile '{tmp_path / 'cfg' / 'client-kit.json'}' -HelperPort {_free_port()}\r\n",
        )
        script = tmp_path / "setup.ps1"
        script.write_text(ps_part, encoding="ascii")
        run = subprocess.run(
            [PWSH, "-NoProfile", "-File", str(script)],
            env=dict(os.environ, TEMP=str(tmp_path), POLARON_CLIENT_KIT_SCRIPTS_DIR=str(kit_scripts)),
            capture_output=True, text=True, timeout=600,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert (client_root / "scripts" / "host-drive-helper.ps1").read_bytes() == (kit_scripts / "host-drive-helper.ps1").read_bytes()
        assert (client_root / "scripts" / "polaron-client-update.ps1").is_file()
        assert (client_root / "backend" / "app" / "routers" / "client_kit.py").read_bytes() == (
            ROOT / "backend" / "app" / "routers" / "client_kit.py"
        ).read_bytes()
        assert not (client_root / "scripts" / "_debug_job_734.py").exists()
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_client_kit_router_is_mounted_for_forensic_and_mobile():
    src = (ROOT / "backend" / "app" / "app_factory.py").read_text(encoding="utf-8")
    assert src.count("app.include_router(client_kit.router)") == 2


def test_ensure_updates_from_the_server_and_never_restarts_during_a_collection():
    ensure = (ROOT / "scripts" / "ensure-host-drive-helper.ps1").read_text(encoding="utf-8")
    assert 'ExpectedHelperVersion = "5.64"' in ensure
    kit = ensure.index("polaron-client-update.ps1")
    quick = ensure.index("$alreadyCurrent = $false")
    assert kit < quick  # the update check runs before the "already current" early exit
    assert "-Watchdog" in ensure and "if ($kitExit -eq 10)" in ensure and "-SkipKitUpdate" in ensure
    guard = ensure.index("if (Test-CollectionRunning) {")
    assert guard < ensure.index("    $ForceRestart = $true\n}")
    helper = (ROOT / "scripts" / "host-drive-helper.ps1").read_text(encoding="utf-8")
    assert 'HelperVersion = "5.64"' in helper
    assert '",\\"client_agent_running\\":"' in helper
