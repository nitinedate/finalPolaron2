"""Static deployment regressions for cached local/prod startup.

These tests don't claim Docker/Windows is available in the test host.
"""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
START = (ROOT / 'script_docker/start_docker.ps1').read_text(encoding='utf-8')
LIB = (ROOT / 'script_docker/start_docker.lib.ps1').read_text(encoding='utf-8')


def load_compose(rel):
    return yaml.safe_load((ROOT / rel).read_text(encoding='utf-8'))


def test_official_dockerhub_chainguard_minio_used_in_both_shared_stacks():
    for rel in ('common-docker/docker-compose.yml', 'services/common/docker-compose.yml'):
        comp = load_compose(rel)
        assert comp['services']['minio']['image'] == '${COMMON_MINIO_IMAGE:-chainguard/minio:latest}'
        assert comp['services']['minio']['volumes'][0]['target'] == '/data'
        assert 'healthcheck' not in comp['services']['minio']  # no Bash in distroless image
    # A cached cgr.dev image may be retagged locally; default pulls use Docker Hub.
    assert "docker @('pull','cgr.dev/chainguard/minio:latest')" not in START
    assert "Wait-MinIOHttpReady" in START
    assert '/minio/health/live' in START


def test_single_optional_gpu_probe_in_common_not_autostarted():
    for rel in ('common-docker/docker-compose.yml', 'services/common/docker-compose.yml'):
        probe = load_compose(rel)['services']['nvidia-gpu-check']
        assert probe['profiles'] == ['gpu-check']
        assert probe['gpus'] == 'all'
    for rel in ('docker-compose.yml', 'services/mobile-android/docker-compose.yml',
                'services/mobile-ios/docker-compose.yml', 'services/mobile-extract/docker-compose.yml'):
        spec = load_compose(rel)['services']
        assert 'nvidia-cuda' not in spec
        assert 'nvidia-smi' not in spec
        for name, service in spec.items():
            depends = service.get('depends_on') or {}
            if isinstance(depends, dict):
                assert 'nvidia-cuda' not in depends, (rel, name)
            elif isinstance(depends, list):
                assert 'nvidia-cuda' not in depends, (rel, name)
        # At least one real GPU worker still gets direct GPU device access.
        assert any(v.get('gpus') == 'all' for v in spec.values())


def test_regular_start_checks_cache_stops_apps_builds_and_restarts():
    assert 'function Test-ApplicationImagesCurrent' in START
    assert 'function Ensure-StartupImageCache' in START
    assert '    Stop-PolaronStacks' in START
    assert '    Invoke-BuildProdApplications -BuildId $buildId' in START
    assert START.index('    Ensure-StartupImageCache') < START.index('    Stop-PolaronStacks')
    assert START.index('    Stop-PolaronStacks') < START.index('    Invoke-BuildProdApplications -BuildId $buildId')
    assert "if ($Recreate) {" in START
    assert "if ($env:AETHERIS_PREFETCH_ALL_IMAGES -eq '1' -and ($RefreshDependencies -or $NoCache))" in START
    assert "if ($Name -eq 'common')" in START
    assert "Start-SharedCommonServices -ComposeArgs $ComposeArgs" in START
    assert "'--no-recreate'" in START
    assert 'Remove-LegacyGpuSidecars' in START
    assert '--no-build' in START
    assert 'Get-StableAppBuildId' in LIB
    assert 'Restart cached HTTPS gateway (no ACME challenge)' in START


def test_both_cmds_accept_maintenance_flags_and_preserve_shared_stack():
    for rel, profile in (('script_docker/start_docker_local.cmd', 'local'),
                         ('script_docker/start_docker_prod.cmd', 'prod')):
        script = (ROOT / rel).read_text(encoding='utf-8')
        assert f'-Profile {profile} %*' in script
        assert 'services\\common\\docker-compose.yml' in script
        assert 'common-docker\\docker-compose.yml' not in script
        assert 'check cached packages, stop Polaron apps, compile, restart' in script
        assert 'prune' in script.lower() or 'preserves' in script.lower()
