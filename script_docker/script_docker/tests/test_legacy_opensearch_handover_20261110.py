from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PS=(ROOT/'script_docker/start_docker.ps1').read_text(encoding='utf-8-sig')

def test_handover_is_opt_in_and_refuses_default():
    assert '[switch]$HandoverLegacyOpenSearch' in PS
    assert 'if (-not $HandoverLegacyOpenSearch)' in PS
    assert 'start_docker_local.cmd -HandoverLegacyOpenSearch' in PS

def test_only_legacy_opensearch_is_stopped():
    assert "$project -ne 'aetheris-forensic' -or $service -ne 'opensearch'" in PS
    assert 'docker stop --time 60 $owner.Id' in PS
    assert 'docker start $owner.Id' in PS
    assert 'docker stop --time 60 ([string]$id)' in PS
    assert 'Get-LegacyOpenSearchVolumeOwner' in PS

def test_switch_forwarded_from_cmds():
    for name in ['start_docker_local.cmd','start_docker_prod.cmd']:
        t=(ROOT/'script_docker'/name).read_text()
        assert '%*' in t

def test_common_volume_still_external():
    t=(ROOT/'services/common/docker-compose.yml').read_text()
    assert 'name: aetheris-forensic_opensearch_data' in t
    assert 'external: true' in t

def test_modern_forensic_compose_does_not_start_legacy_opensearch():
    t=(ROOT/'services/forensic/docker-compose.yml').read_text()
    assert '\n  opensearch:' not in t
