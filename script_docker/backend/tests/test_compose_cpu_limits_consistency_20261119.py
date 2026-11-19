"""Prevent mixed CPU-limit declarations that block start_docker_local.cmd.

Compose requires service-level ``cpus`` and ``deploy.resources.limits.cpus``
to agree. We keep one source of truth: the deployment limit. These tests do
not require Docker Desktop, live evidence, or a scanner.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CPU_LIMITED_COMPOSE = {
    'docker-compose.yml': 13,
    'services/vuln/docker-compose.yml': 2,
    'laptop-scanner/docker-compose.yml': 5,
}


def _read_services(relative_path: str):
    compose = yaml.safe_load((ROOT / relative_path).read_text(encoding='utf-8'))
    assert isinstance(compose, dict)
    assert isinstance(compose.get('services'), dict)
    return compose['services']


@pytest.mark.parametrize('compose_path,expected_count', CPU_LIMITED_COMPOSE.items())
def test_cpu_limits_have_one_authoritative_field(compose_path, expected_count):
    services = _read_services(compose_path)
    limited = 0
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        direct = service.get('cpus')
        deploy = (((service.get('deploy') or {}).get('resources') or {})
                  .get('limits') or {}).get('cpus')
        assert direct is None, f'{compose_path}:{name} must not use top-level cpus'
        if deploy is None:
            continue
        limited += 1
        # These parameters retain their exact original .env substitution key.
        assert isinstance(deploy, str)
        assert re.fullmatch(r'\$\{[A-Z0-9_]+_CPU_[A-Z0-9_]+_CPUS:-[0-9]+(?:\.[0-9]+)?\}', deploy), (
            compose_path, name, deploy
        )
    assert limited == expected_count, f'{compose_path}: a CPU budget was removed'


@pytest.mark.parametrize('compose_path,expected_cores', [
    ('docker-compose.yml', 9.25),
    ('services/vuln/docker-compose.yml', 1.25),
    ('laptop-scanner/docker-compose.yml', 4.15),
])
def test_default_cpu_ceiling_is_preserved(compose_path, expected_cores):
    services = _read_services(compose_path)
    defaults = []
    for service in services.values():
        if not isinstance(service, dict):
            continue
        cpus = (((service.get('deploy') or {}).get('resources') or {})
                .get('limits') or {}).get('cpus')
        if cpus is not None:
            defaults.append(float(cpus.split(':-')[1].rstrip('}')))
    assert sum(defaults) == pytest.approx(expected_cores)


def test_forensic_drive_overlay_only_adds_mounts_not_second_cpu_budget():
    overlay = _read_services('docker-compose.drives.forensic.yml')
    for name, service in overlay.items():
        assert 'cpus' not in service, name
        assert 'deploy' not in service, name
