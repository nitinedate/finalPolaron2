"""V1.29: start_docker_prod.cmd serves HTTPS on the .env IP (and optional domain) with auto-renewal."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_prod_launcher_names_the_env_settings_and_keeps_local_lines():
    prod = read("script_docker/start_docker_prod.cmd")
    local = read("script_docker/start_docker_local.cmd")
    assert "-Profile prod %*" in prod and "PRODUCTION HTTPS" in prod and "production TLS" in prod
    assert "PUBLIC_HTTPS_IP" in prod and "PUBLIC_HTTPS_DOMAIN" in prod and "renewed automatically" in prod
    assert "start_docker.settings.json" not in prod
    # Same stack description as the local launcher, apart from the address lines.
    for line in local.splitlines():
        if line.startswith(("echo  Common", "echo  Neo4j", "echo  AI ", "echo  Models", "echo  Evidence",
                            "echo  Build", "echo  Cache", "echo  Legacy", "echo             MailHog")):
            assert line in prod, line
    raw = (ROOT / "script_docker" / "start_docker_prod.cmd").read_bytes()
    assert raw.count(b"\r\n") > 20 and all(b < 128 for b in raw)
    for ch in "&|<>":
        assert not any(ch in l for l in prod.splitlines() if l.startswith("echo ")), ch


def test_env_example_documents_ip_domain_and_email():
    env = read(".env.example")
    for key in ("PUBLIC_HTTPS_IP=", "PUBLIC_HTTPS_DOMAIN=", "PUBLIC_HTTPS_EMAIL="):
        assert re.search(rf"(?m)^{key}$", env), key
    assert "Leave empty for IP-only" in env


def test_settings_come_from_env_for_prod_and_prod_accepts_a_domain():
    lib = read("script_docker/start_docker.lib.ps1")
    body = lib.split("function Get-DeploymentSettings {", 1)[1].split("function Save-DeploymentSettings {", 1)[0]
    assert "Read-DotEnvValues -Root $Root" in body
    assert "$dotEnv['PUBLIC_HTTPS_IP']" in body and "$dotEnv.ContainsKey('PUBLIC_HTTPS_DOMAIN')" in body
    assert "PUBLIC_HTTPS_EMAIL" in body
    assert "The prod launcher uses only an IP address" not in body
    assert '$Profile -eq "prod" -and $dns -and -not (Test-PublicDomainName $dns)' in body
    save = lib.split("function Save-DeploymentSettings {", 1)[1].split("function Get-ComposePullFlag {", 1)[0]
    assert "IPFromDotEnv" in save and "DomainFromDotEnv" in save
    # .env is read, never written, by the launcher.
    reader = lib.split("function Read-DotEnvValues {", 1)[1].split("function Test-PublicDomainName {", 1)[0]
    assert "ReadAllLines" in reader and "Write" not in reader


def test_https_gateway_has_the_local_gateway_routes_and_errors():
    inc = read("deployment/windows-ip-https/gateway-https-locations.inc")
    local = read("frontend/nginx-gateway.conf")
    for route in ("/api/acquisition/", "/api/malware/", "/api/vuln/", "/api/scanners", "/api/scanner-agent/",
                  "/api/scan-jobs", "/api/scan-policies", "/api/assets", "/api/remediation-tasks", "/api/ "):
        assert f"location {route}" in local and f"location {route}" in inc, route
    assert inc.count("error_page 502 = @api_unreachable;") == local.count("error_page 502 = @api_unreachable;")
    assert "location @api_unreachable" in inc and "location @malware_unreachable" in inc
    assert "proxy_read_timeout 10s;" in inc  # /health like local
    lib = read("script_docker/start_docker.lib.ps1")
    nginx = lib.split("function Get-PublicNginxConfig {", 1)[1].split("function Assert-PublicPortOwners {", 1)[0]
    assert "map `$http_x_aetheris_service `$svc_id" in nginx and "map `$svc_id `$svc_label" in nginx
    assert "map `$host `$aetheris_https_host" in nginx
    assert "return 308 https://`$aetheris_https_host`$request_uri;" in nginx
    assert "/.well-known/acme-challenge/" in nginx  # HTTP-01 stays on public TCP 80


def test_renewal_is_automatic_and_limited_to_served_certificates():
    renew = read("script_docker/renew-certificates.sh")
    assert "certbot renew" in renew and "21600" in renew and "--deploy-hook 'kill -HUP 1'" in renew
    assert 'for name in $AETHERIS_CERT_NAMES' in renew and '--cert-name "$name"' in renew
    assert "*[!a-z0-9.-]*" in renew
    assert b"\r\n" not in (ROOT / "script_docker" / "renew-certificates.sh").read_bytes()
    compose = read("deployment/windows-ip-https/docker-compose.gateway-public-https.yml")
    assert "AETHERIS_CERT_NAMES: ${AETHERIS_CERT_NAMES:-}" in compose
    assert "restart: unless-stopped" in compose and 'pid: "service:gateway"' in compose


def test_edge_issues_identities_separately_and_serves_only_existing_certificates():
    ps = read("script_docker/start_docker.ps1")
    edge = ps.split("function Ensure-PublicHttpsEdge {", 1)[1].split("if ($env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY", 1)[0]
    assert "Test-PublicCertificatesCurrent" in edge and "Set-RenewerCertificateNames" in edge
    assert "foreach ($entry in $wanted)" in edge and "$failed[$entry.Name] = $true" in edge
    assert "$script:AetherisServedDomain = ''" in edge  # missing domain certificate -> IP only
    assert "no certificate is stored" in edge
    assert "'--force-recreate', 'certbot-renewer'" in edge
    main = ps.split("if ($env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY -eq '1') { return }", 1)[1]
    assert "$env:APP_BASE_URL = Get-PublicAppBaseUrl -Root $root -HttpsUrl" in main
    assert main.index("Ensure-PublicHttpsEdge -IP") < main.index("$env:APP_BASE_URL = Get-PublicAppBaseUrl") < main.index('Write-Step "Start product stacks"')
    assert 'READY: https://$($script:AetherisServedDomain)/' in main
    for rel in ("script_docker/start_docker.ps1", "script_docker/start_docker.lib.ps1"):
        data = (ROOT / rel).read_bytes()
        assert all(b < 128 for b in data) and b"\r\n" in data, rel


def test_offline_powershell_checks_ship_with_the_release():
    edge = read("tests/prod_https_edge_checks.ps1")
    assert "AETHERIS_START_DOCKER_FUNCTIONS_ONLY" in edge and "PASS: $script:checks production HTTPS edge checks" in edge
    checks = read("tests/windows_docker_startup_checks.ps1")
    assert "'Production accepts an optional domain (normalized)'" in checks
    assert "'Empty PUBLIC_HTTPS_DOMAIN means IP-only HTTPS'" in checks
