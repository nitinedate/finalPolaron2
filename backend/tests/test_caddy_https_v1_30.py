"""V1.30: when Caddy already serves TCP 80/443, start_docker_prod.cmd adds Polaron to Caddy."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_env_example_documents_the_caddy_mode():
    env = read(".env.example")
    assert re.search(r"(?m)^PUBLIC_HTTPS_PROXY=auto$", env)
    for key in ("CADDY_EXE=", "CADDY_CONFIG=", "PUBLIC_HTTPS_UPSTREAM=", "CADDY_MANAGE_CONFIG="):
        assert key in env, key


def test_caddy_is_changed_before_containers_stop_and_replaces_the_builtin_edge():
    ps = read("script_docker/start_docker.ps1")
    main = ps.split("if ($env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY -eq '1') { return }", 1)[1]
    assert main.index("Publish-CaddySite -Caddy") < main.index("    Stop-PolaronStacks")
    assert "Start-CaddyBackedGateway" in main and "Test-CaddyServedHttps" in main
    edge = main.split("if ($isPublic -and $script:HttpsProxyMode -eq 'caddy') {", 1)[1][:400]
    assert "Start-CaddyBackedGateway" in edge and "Ensure-PublicHttpsEdge" in edge  # elseif keeps builtin
    publish = ps.split("function Publish-CaddySite {", 1)[1].split("function Start-CaddyBackedGateway {", 1)[0]
    assert "'validate', '--config'" in publish and "'reload', '--config'" in publish
    assert publish.index("'validate'") < publish.index("'reload'")
    assert "WriteAllBytes($configPath, $oldConfig)" in publish  # restore on failure
    assert "polaron-backup-" in publish
    gateway = ps.split("function Start-CaddyBackedGateway {", 1)[1].split("function Test-CaddyServedHttps {", 1)[0]
    assert "Get-LocalGatewayArgs" in gateway and "'rm', '-sf', 'acme-bootstrap', 'certbot-renewer'" in gateway


def test_caddy_site_file_requests_ip_certificates_correctly():
    lib = read("script_docker/start_docker.lib.ps1")
    site = lib.split("function Get-CaddySiteConfig {", 1)[1].split("function Test-CaddyImportPresent {", 1)[0]
    for needle in ("issuer acme https://acme-v02.api.letsencrypt.org/directory", "profile shortlived",
                   "disable_tlsalpn_challenge", "reverse_proxy $Upstream", "flush_interval -1"):
        assert needle in site, needle
    imp = lib.split("function Add-CaddyImportLine {", 1)[1].split("function Get-PublicNginxConfig {", 1)[0]
    assert "'import polaron*.caddy'" in imp  # a glob: a missing file never stops Caddy


def test_offline_caddy_checks_ship_with_the_release():
    checks = read("tests/caddy_integration_checks.ps1")
    assert "PASS: $script:checks Caddy integration checks" in checks
    cmd = read("script_docker/start_docker_prod.cmd")
    assert "PUBLIC_HTTPS_PROXY" in cmd and "Caddy" in cmd
