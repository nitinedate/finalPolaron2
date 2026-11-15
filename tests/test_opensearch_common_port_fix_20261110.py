"""Static regression coverage for Windows OpenSearch host-port repair and shared reuse."""
from pathlib import Path
import re
import yaml

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=(ROOT/'script_docker/start_docker.ps1').read_text(encoding='utf-8')
HELPER=(ROOT/'scripts/ensure_common_opensearch.ps1').read_text(encoding='utf-8')


def test_both_shared_compose_variants_default_to_non_conflicting_host_port():
    for name in ('services/common/docker-compose.yml','common-docker/docker-compose.yml'):
        text=(ROOT/name).read_text()
        doc=yaml.safe_load(text)
        ports=doc['services']['opensearch']['ports']
        assert ports == ['127.0.0.1:${COMMON_OPENSEARCH_PORT:-19200}:9200']
        assert 'opensearch_data' in doc['services']['opensearch']['volumes'][0]
        assert doc['services']['opensearch']['image'] == 'opensearchproject/opensearch:2'


def test_open_search_in_container_url_keeps_9200():
    text=(ROOT/'.env.example').read_text()
    assert 'OPENSEARCH_URL=http://opensearch:9200' in text
    assert 'COMMON_OPENSEARCH_PORT=19200' in text
    assert 'COMMON_OPENSEARCH_PORT=19200' in (ROOT/'common-docker/.env.example').read_text()
    assert 'OPENSEARCH_URL=http://localhost:9200' not in text


def test_preflight_runs_before_any_application_stop():
    body=SCRIPT.split("if ($env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY -eq '1') { return }",1)[1]
    assert body.index("& $opensearchPortGuard -ProjectRoot $root") < body.index('    Stop-PolaronStacks')
    assert body.index('Start-SharedCommonServices -ComposeArgs $commonPreflightArgs') < body.index('    Stop-PolaronStacks')
    assert body.index('    Ensure-StartupImageCache') < body.index('    Stop-PolaronStacks')
    assert 'Wait-ComposeServiceReady -ProjectName \'aetheris-common\' -Service \'opensearch\'' in body
    assert 'shared OpenSearch host port' in body


def test_port_repair_persists_choice_and_preserves_config():
    assert 'Test-LoopbackPortFree' in HELPER
    assert '19200..19250' in HELPER and '19300..19320' in HELPER
    assert 'COMMON_OPENSEARCH_PORT' in HELPER
    assert 'OPENSEARCH_URL' in HELPER
    assert 'opensearch:9200' in HELPER
    assert '.bak' in HELPER
    assert 'Copy-Item -LiteralPath $envFile' in HELPER
    assert 'docker ps -q --filter' in HELPER
    assert 'if ($running.Count -gt 0' in HELPER
    assert 'docker stop' not in HELPER
    assert 'docker volume' not in HELPER


def test_no_wholesale_recreation_of_shared_services():
    body=SCRIPT.split('function Start-SharedCommonServices',1)[1].split('function Invoke-StackUp',1)[0]
    assert "'--no-recreate'" in body
    assert "'--no-deps'" in body
    assert "'--force-recreate'" in body
    assert "$service -eq 'opensearch'" in body
    assert 'Reuse running Common Docker' in body
    assert 'postgres-ensure' in body
    assert 'Get-SharedOpenSearchContainerPort' in body
    assert 'Get-ConfiguredSharedOpenSearchPort' in body
    assert 'Wait-ComposeServiceReady' in body
    assert 'docker volume rm' not in body
    assert 'down' not in re.findall(r"(?m)^\s*(?:& docker |Invoke-Docker ).+$",body).__str__()


def test_both_launchers_still_call_single_updated_script():
    for name, profile in (('start_docker_local.cmd','local'),('start_docker_prod.cmd','prod')):
        text=(ROOT/'script_docker'/name).read_bytes().decode('ascii')
        assert f'-Profile {profile} %*' in text
        assert 'start_docker.ps1' in text
        assert 'common-docker\\docker-compose.yml' not in text


def test_new_port_preflight_is_required_deployment_file():
    import json
    manifest=json.loads((ROOT/'script_docker/start_docker.required-files.json').read_text())
    assert 'scripts/ensure_common_opensearch.ps1' in manifest['common']


def test_legacy_index_volume_cannot_be_mounted_by_two_servers():
    assert 'function Assert-SharedOpenSearchVolumeExclusive' in SCRIPT
    guard=SCRIPT.split('function Assert-SharedOpenSearchVolumeExclusive',1)[1].split('function Start-SharedCommonServices',1)[0]
    assert "'aetheris-forensic_opensearch_data'" in guard
    assert 'Get-LegacyOpenSearchVolumeOwner' in guard
    assert 'if (-not $HandoverLegacyOpenSearch)' in guard
    assert 'docker stop --time 60 $owner.Id' in guard
    assert 'Restore-LegacyOpenSearchHandover' in guard
    body=SCRIPT.split('function Start-SharedCommonServices',1)[1].split('function Invoke-StackUp',1)[0]
    assert 'Assert-SharedOpenSearchVolumeExclusive' in body
