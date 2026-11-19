"""Source-level regressions for the Windows cached package / build / restart flow.

Windows Docker Desktop is not available in CI; these checks verify the release
contract without falsely claiming to execute a live Windows deployment.
"""
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'script_docker/start_docker.ps1').read_text('utf-8')
LIB = (ROOT / 'script_docker/start_docker.lib.ps1').read_text('utf-8-sig')
MAIN = SCRIPT.split("if ($env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY -eq '1') { return }", 1)[1]


def test_cache_preflight_before_stop_before_compile_before_start():
    order = (
        'Assert-AllComposeFiles -CreateRuntimeDirectories',
        'Ensure host SuryaOCR inference',
        '    Ensure-StartupImageCache',
        '    Stop-PolaronStacks',
        '    Invoke-BuildProdApplications -BuildId $buildId',
        '    Invoke-StackUp -Name \'common\'',
        'Start product stacks',
    )
    positions = [MAIN.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_normal_start_always_builds_using_cache_not_no_cache():
    assert 'Invoke-BuildProdApplications -BuildId $buildId' in MAIN
    assert 'if ($needBuild)' not in MAIN
    assert '$env:DOCKER_BUILDKIT = \'1\'' in MAIN
    assert 'Get-StableAppBuildId -Root $root' in MAIN
    assert '$script:AetherisImagePullPolicy = \'missing\'' in MAIN
    assert "if ($RefreshDependencies -or $NoCache) { $dockerArgs += @('--pull', '--no-cache') }" in SCRIPT
    assert 'function Invoke-BuildProdApplications' in SCRIPT
    assert "'--target', 'dependencies'" in SCRIPT


def test_preflight_pulls_missing_images_only_and_before_shutdown():
    body = SCRIPT.split('function Ensure-StartupImageCache', 1)[1].split('function Add-UniqueImageName', 1)[0]
    assert 'if (Test-DockerImagePresent -Image $image)' in body
    assert 'Download missing image once: $image' in body
    assert 'Existing application containers remain running.' in body
    assert 'function Get-StartupRequiredImages' in SCRIPT
    assert "'nvidia/cuda:12.6.3-runtime-ubuntu24.04'" in SCRIPT
    assert "'node:20-alpine'" in SCRIPT
    assert "'python:3.12-slim'" in SCRIPT
    assert "@($svc.profiles).Count -gt 0" in SCRIPT
    assert "^(aetheris-|rag_new2-)" in SCRIPT


def test_only_polaron_containers_stopped_and_critical_services_preserved():
    body = SCRIPT.split('function Stop-PolaronStacks', 1)[1].split('function Assert-StartupAssets', 1)[0]
    assert 'Stop-PodmanAetherisContainers' not in body
    assert "'aetheris-common'" not in body
    assert "label=com.docker.compose.project=$name" in body
    assert "label=com.docker.compose.service=gateway" in body
    assert "@('api','worker-nessus')" in body
    assert "@('stop', '--time', '45')" in body
    assert 'docker volume rm' not in body
    assert 'docker system prune' not in body
    assert "'certbot-renewer'" not in body
    assert "'redis-gvm'" not in body


def test_prod_reuses_certificates_and_greenbone_not_run_feeds_each_time():
    cert = SCRIPT.split('function Ensure-PublicHttpsEdge', 1)[1].split('if ($env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY', 1)[0]
    assert "Restart cached HTTPS gateway (no ACME challenge)" in cert
    assert "if ($renewer -and $renewer.Status -eq 'running')" in cert
    vuln = SCRIPT.split('function Start-VulnerabilityStackPhased', 1)[1].split('function Get-CommonRedisState', 1)[0]
    assert "foreach ($service in @('redis-gvm','pg-gvm','gvmd','openvas','openvasd','ospd-openvas'))" in vuln
    assert 'Reused Greenbone engine and feeds' in vuln
    assert 'Restart vulnerability worker from cached image' in vuln
    assert 'Initialize Greenbone feeds sequentially' in vuln


def test_both_cmd_files_route_same_build_restart_and_keep_windows_crlf():
    for name, profile in [('start_docker_local.cmd','local'), ('start_docker_prod.cmd','prod')]:
        path = ROOT / 'script_docker' / name
        blob = path.read_bytes()
        assert b'\r\n' in blob
        assert all(x < 128 for x in blob)
        script = blob.decode('ascii')
        assert f'-Profile {profile} %*' in script
        assert 'check cached packages, stop Polaron apps, compile, restart' in script
        assert 'reuse CUDA, Python, pip, npm and Docker image layers' in script
        assert 'services\\common\\docker-compose.yml' in script
        assert 'common-docker\\docker-compose.yml' not in script
    ps = (ROOT / 'script_docker/start_docker.ps1').read_bytes()
    assert b'\r\n' in ps and all(x < 128 for x in ps)


def test_packages_in_dockerfiles_separate_from_compiled_application_stage():
    for rel in ('backend/Dockerfile', 'backend/Dockerfile.malware', 'backend/Dockerfile.scanner'):
        dockerfile = (ROOT / rel).read_text()
        assert ' AS dependencies' in dockerfile
        assert 'FROM dependencies AS application' in dockerfile or 'FROM malware-dependencies AS application' in dockerfile
        assert dockerfile.index('COPY requirements') < dockerfile.index('FROM dependencies AS application') if 'FROM dependencies AS application' in dockerfile else True
        assert 'AETHERIS_APP_BUILD_ID' in dockerfile
    frontend = (ROOT / 'frontend/Dockerfile').read_text()
    assert 'FROM node:20-alpine AS dependencies' in frontend
    assert 'npm ci --no-audit --no-fund' in frontend
    assert 'FROM dependencies AS build' in frontend
    assert 'npm run "build:${VITE_AETHERIS_SERVICE}"' in frontend


def test_common_minio_pgadmin_resources_stay_external():
    for rel in ('common-docker/docker-compose.yml', 'services/common/docker-compose.yml'):
        d = yaml.safe_load((ROOT / rel).read_text())
        assert d['services']['minio']['image'] == '${COMMON_MINIO_IMAGE:-chainguard/minio:latest}'
        assert d['services']['pgadmin']['volumes']
    assert "if ($Name -eq 'common')" in SCRIPT
    assert "Start-SharedCommonServices -ComposeArgs $ComposeArgs" in SCRIPT
    assert "'--no-recreate'" in SCRIPT
    assert "Invoke-DownPolaronProjects" in SCRIPT
    assert "if ($stack.Name -eq 'common') { continue }" in SCRIPT
