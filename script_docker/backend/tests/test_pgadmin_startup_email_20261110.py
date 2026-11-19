"""Offline regression checks for the shared pgAdmin email restart-loop repair.

Run: PYTHONPATH=. pytest -q tests/test_pgadmin_startup_email_20261110.py
These tests do not replace a Windows/Compose integration test.
"""
import json
from pathlib import Path

import pytest
import yaml
from email_validator import validate_email, EmailNotValidError

ROOT = Path(__file__).resolve().parents[2]
VALID = 'admin@forensic.example.com'


def test_rejected_legacy_default_is_actually_invalid():
    with pytest.raises(EmailNotValidError):
        validate_email('admin@example.test', check_deliverability=False)


def test_new_default_passes_pgadmin_validator():
    assert validate_email(VALID, check_deliverability=False).normalized == VALID


@pytest.mark.parametrize('relative', ['.env.example', 'common-docker/.env.example'])
def test_env_templates_have_accepted_default(relative):
    text = (ROOT / relative).read_text('utf-8')
    assert f'PGADMIN_DEFAULT_EMAIL={VALID}' in text
    assert 'PGADMIN_DEFAULT_EMAIL=admin@example.test' not in text


@pytest.mark.parametrize('relative', [
    'services/common/docker-compose.yml',
    'common-docker/docker-compose.yml',
    'docker-compose.https.yml',
])
def test_compose_all_pgadmin_fallbacks_are_valid(relative):
    spec = yaml.safe_load((ROOT / relative).read_text('utf-8'))
    pgadmin = spec['services']['pgadmin']
    value = pgadmin['environment']['PGADMIN_DEFAULT_EMAIL']
    assert value == '${PGADMIN_DEFAULT_EMAIL:-' + VALID + '}'
    assert pgadmin['volumes'], 'Existing pgAdmin data volume must be preserved'
    assert pgadmin['restart'] != 'always', 'Avoid endless restart storm on a bad configuration'


def test_startup_repairs_stale_env_before_compose_preflight():
    source = (ROOT / 'script_docker/start_docker.ps1').read_text('utf-8')
    assert source.index('Initialize-DeploymentFiles -Root $root') < source.index("$pgadminEnvRepair = Join-Path")
    assert source.index("& $pgadminEnvRepair -ProjectRoot $root") < source.index('    Assert-AllComposeFiles -CreateRuntimeDirectories')
    assert source.index("& $pgadminEnvRepair -ProjectRoot $root") < source.index('    if ($PrepareOnly)')


def test_repair_is_scoped_to_the_legacy_placeholder_only():
    source = (ROOT / 'scripts/ensure_pgadmin_email.ps1').read_text('utf-8')
    assert "$legacy = 'admin@example.test'" in source
    assert "$replacement = 'admin@forensic.example.com'" in source
    assert 'if ($configured -ieq $legacy)' in source
    assert 'Copy-Item -LiteralPath $Path -Destination $backup' in source
    assert "if ($effective -ieq $legacy)" in source
    assert 'Remove-Item' not in source
    assert 'docker volume rm' not in source.lower()


def test_manifest_includes_pgadmin_preflight():
    obj = json.loads((ROOT / 'script_docker/start_docker.required-files.json').read_text('utf-8'))
    assert 'scripts/ensure_pgadmin_email.ps1' in obj['common']


def test_one_command_repair_only_recreates_pgadmin():
    source = (ROOT / 'script_docker/repair_pgadmin.cmd').read_text('utf-8')
    assert '--force-recreate pgadmin' in source
    assert '--no-deps' in source
    assert 'down -v' not in source
    assert 'volume prune' not in source


def test_both_original_launchers_remain_present():
    for f in ('start_docker_local.cmd', 'start_docker_prod.cmd'):
        s = (ROOT / 'script_docker' / f).read_text('utf-8')
        assert 'start_docker.ps1' in s
        assert '-Profile' in s
