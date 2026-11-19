from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = (ROOT / 'script_docker/start_docker.lib.ps1').read_text(encoding='utf-8-sig')
START = (ROOT / 'script_docker/start_docker.ps1').read_text(encoding='utf-8-sig')
COMPOSE = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8-sig')


def test_retired_ollama_and_opensearch_containers_excluded_before_health_check():
    assert "@('ollama', 'ollama-init', 'opensearch')" in LIB
    pos = LIB.index("if ($project -eq 'aetheris-forensic' -and $service -in $legacyForensicServices) { continue }")
    assert pos < LIB.index('if ($status -eq "running"', pos)
    assert 'if ($service -in $oneShots' in LIB


def test_running_services_still_subject_to_readiness_failure():
    assert '$failed += "$($item.Name) status=$status health=$health exit=$($item.State.ExitCode)"' in LIB
    assert 'if ($report.Failed.Count -gt 0)' in START


def test_host_ollama_checked_from_forensic_api_container():
    assert "os.environ.get('OLLAMA_BASE_URL','http://host.docker.internal:11434')" in START
    assert "'/api/tags'" in START
    assert 'docker exec $apiContainer[0] python -c $ollamaProbe' in START
    assert 'No Docker Ollama is required.' in START


def test_current_compose_uses_host_ollama_and_shared_opensearch():
    assert '${OLLAMA_BASE_URL:-http://host.docker.internal:11434}' in COMPOSE
    assert '${OPENSEARCH_URL:-http://opensearch:9200}' in COMPOSE
