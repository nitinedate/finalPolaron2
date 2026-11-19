"""Regression: Windows CMD guard must match the Compose file used by the PS launcher."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_guards_match_authoritative_compose_path():
    for launcher in ("start_docker_local.cmd", "start_docker_prod.cmd"):
        data = (ROOT / "script_docker" / launcher).read_bytes()
        assert data.startswith(b"@echo off\r\n")
        assert b"\r\n" in data
        assert b"%ROOT%\\services\\common\\docker-compose.yml" in data
        assert b"%ROOT%\\common-docker\\docker-compose.yml" not in data
        assert b"missingcommon" in data


def test_launcher_and_manifest_match_runtime_compose_file():
    lib = (ROOT / "script_docker/start_docker.lib.ps1").read_text(encoding="utf-8-sig")
    manifest = (ROOT / "script_docker/start_docker.required-files.json").read_text()
    assert '"services/common/docker-compose.yml"' in manifest
    assert '"common-docker/docker-compose.yml"' not in manifest
    assert '"services/$Name/docker-compose.yml"' in lib
    assert (ROOT / "services/common/docker-compose.yml").is_file()


def test_legacy_and_standalone_shared_composes_include_required_bind_mounts():
    for rel, paths in (
        ("services/common/docker-compose.yml", (
            "docker/postgres/init/01_extensions.sql",
            "docker/postgres/init/02_common_databases.sql",
            "docker/pgadmin/servers.json",
            "docker/pgadmin/pgpass",
        )),
        ("common-docker/docker-compose.yml", (
            "common-docker/postgres/init/01_extensions.sql",
            "common-docker/postgres/init/02_common_databases.sql",
            "common-docker/pgadmin/servers.json",
            "common-docker/pgadmin/pgpass",
        )),
    ):
        assert (ROOT / rel).is_file()
        for path in paths:
            assert (ROOT / path).is_file(), path
