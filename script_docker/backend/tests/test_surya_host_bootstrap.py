"""Offline checks for the host-native Surya launcher contract."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def bootstrap():
    path = ROOT / 'scripts/surya_host_bootstrap.py'
    spec = importlib.util.spec_from_file_location('surya_host_bootstrap_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vulkan_release_preferred(bootstrap):
    assets = [
        {'name':'llama-b1-bin-win-cpu-x64.zip','browser_download_url':'https://example.test/cpu'},
        {'name':'llama-b1-bin-win-vulkan-x64.zip','browser_download_url':'https://example.test/vulkan'},
    ]
    assert bootstrap.choose_asset(assets, 'vulkan')[0].endswith('/vulkan')
    assert bootstrap.choose_asset(assets, 'cpu')[0].endswith('/cpu')


def test_missing_windows_release_is_explicit(bootstrap):
    with pytest.raises(RuntimeError, match='llama.cpp'):
        bootstrap.choose_asset([{'name':'linux-x64.zip','browser_download_url':'https://example.test/linux'}], 'vulkan')


def test_surya_wiring_and_no_glm():
    docker = (ROOT / 'docker-compose.yml').read_text()
    startup = (ROOT / 'script_docker/start_docker.ps1').read_text()
    adapter = (ROOT / 'backend/app/services/surya_adapter.py').read_text()
    assert 'SURYA_INFERENCE_BACKEND:-llamacpp' in docker
    assert 'ensure_host_surya.ps1' in startup
    assert startup.index('Ensure host SuryaOCR inference') < startup.index('    Stop-PolaronStacks')
    assert 'Verify Surya inference is reachable from forensic API container' in startup
    assert '"llamacpp"' in adapter
    assert 'glm-ocr' not in startup.lower()


def test_existing_custom_endpoint_preserved_by_migration():
    script = (ROOT / 'scripts/align_surya_env.ps1').read_text()
    assert "$url -ne $old" in script
    assert "mode = 'external'" in script
    assert 'surya-pre-native.bak' in script

# Regression tests for upstream's source-only stable /releases/latest release.
def test_skips_latest_stable_without_windows_binaries(bootstrap):
    releases = [
        {'tag_name': 'v0.9.0', 'prerelease': False, 'draft': False,
         'assets': [{'name': 'nightly-tag.txt', 'browser_download_url': 'https://example.test/text'}]},
        {'tag_name': 'b12241', 'prerelease': True, 'draft': False,
         'assets': [
             {'name': 'llama-b12241-bin-win-vulkan-x64.zip',
              'browser_download_url': 'https://github.com/ggml-org/llama.cpp/releases/download/b12241/llama-b12241-bin-win-vulkan-x64.zip',
              'digest': 'sha256:' + 'a' * 64},
         ]},
    ]
    candidate = bootstrap.choose_release_asset(releases, 'vulkan')
    assert candidate[1] == 'llama-b12241-bin-win-vulkan-x64.zip'
    assert candidate[2] == 'sha256:' + 'a' * 64


def test_discovery_prefers_listed_binary_build_over_source_only_stable(bootstrap, monkeypatch):
    seen = []
    def fake_fetch(url):
        seen.append(url)
        return [
            {'tag_name': 'v0.9.0', 'assets': [{'name': 'nightly-tag.txt'}]},
            {'tag_name': 'b12241', 'assets': [{
                'name': 'llama-b12241-bin-win-vulkan-x64.zip',
                'browser_download_url': 'https://github.com/ggml-org/llama.cpp/releases/download/b12241/llama-b12241-bin-win-vulkan-x64.zip'
            }]}
        ]
    monkeypatch.setattr(bootstrap, 'get_json', fake_fetch)
    monkeypatch.delenv('SURYA_LLAMA_BUILD_TAG', raising=False)
    url, name, digest = bootstrap.select_official_download('vulkan')
    assert name.startswith('llama-b12241') and digest == ''
    assert '/releases/download/b12241/' in url
    assert 'releases?' in seen[0]  # explicitly not /releases/latest


def test_discovery_uses_known_official_build_after_rate_limit(bootstrap, monkeypatch):
    def rate_limited(_):
        raise RuntimeError('API rate limit')
    monkeypatch.setattr(bootstrap, 'get_json', rate_limited)
    monkeypatch.delenv('SURYA_LLAMA_BUILD_TAG', raising=False)
    url, name, digest = bootstrap.select_official_download('cpu')
    assert name == 'llama-b12241-bin-win-cpu-x64.zip'
    assert url.startswith(bootstrap.OFFICIAL_DOWNLOAD_PREFIX)
    assert digest == ''


def test_no_implicit_cpu_binary_when_vulkan_requested(bootstrap):
    import pytest
    with pytest.raises(RuntimeError):
        bootstrap.choose_asset([{
            'name': 'llama-b1-bin-win-cpu-x64.zip',
            'browser_download_url': 'https://github.com/ggml-org/llama.cpp/releases/download/b1/llama-b1-bin-win-cpu-x64.zip'
        }], 'vulkan')


def test_cached_server_does_not_call_github(bootstrap, monkeypatch, tmp_path):
    server = tmp_path / 'llama.cpp' / 'llama-server.exe'
    server.parent.mkdir()
    server.write_bytes(b'MZ' + b'\0' * 2000)
    monkeypatch.delenv('SURYA_LLAMA_SERVER_EXE', raising=False)
    monkeypatch.setattr(bootstrap, 'get_json', lambda _: (_ for _ in ()).throw(AssertionError('GitHub contacted')))
    assert bootstrap.install_server(tmp_path, 'vulkan') == server


def test_staged_server_install_and_windows_dependency_files(bootstrap, monkeypatch, tmp_path):
    import zipfile
    monkeypatch.delenv('SURYA_LLAMA_SERVER_EXE', raising=False)
    selected = ('https://github.com/ggml-org/llama.cpp/releases/download/b12241/llama-b12241-bin-win-vulkan-x64.zip',
                'llama-b12241-bin-win-vulkan-x64.zip', '')
    monkeypatch.setattr(bootstrap, 'select_official_download', lambda _: selected)
    def fake_download(url, target, *, sha256_digest=''):
        with zipfile.ZipFile(target, 'w') as z:
            z.writestr('bin/llama-server.exe', b'MZ' + b'X' * 2000)
            z.writestr('bin/ggml-vulkan.dll', b'library')
            z.writestr('../escape.exe', b'evil')
            z.writestr('ignored/notes.md', b'ignore')
    monkeypatch.setattr(bootstrap, 'download', fake_download)
    server = bootstrap.install_server(tmp_path, 'vulkan')
    assert server.exists()
    assert (server.parent / 'ggml-vulkan.dll').exists()
    assert not (tmp_path / 'escape.exe').exists()
    assert not (tmp_path / 'llama.cpp' / 'notes.md').exists()


def test_unusable_zip_does_not_publish_partial_server(bootstrap, monkeypatch, tmp_path):
    import zipfile
    monkeypatch.delenv('SURYA_LLAMA_SERVER_EXE', raising=False)
    monkeypatch.setattr(bootstrap, 'select_official_download', lambda _: (
        'https://github.com/ggml-org/llama.cpp/releases/download/b12241/llama-b12241-bin-win-cpu-x64.zip',
        'llama-b12241-bin-win-cpu-x64.zip', ''))
    def invalid_zip(_url, target, *, sha256_digest=''):
        with zipfile.ZipFile(target, 'w') as z:
            z.writestr('readme.txt', b'no server')
    monkeypatch.setattr(bootstrap, 'download', invalid_zip)
    with pytest.raises(RuntimeError, match='no usable llama-server.exe'):
        bootstrap.install_server(tmp_path, 'cpu')
    assert not (tmp_path / 'llama.cpp' / 'llama-server.exe').exists()


def test_download_checks_github_digest_and_cleans_partials(bootstrap, monkeypatch, tmp_path):
    import hashlib
    from io import BytesIO
    payload = b'P' * 2048
    class Stub:
        def __enter__(self):
            return BytesIO(payload)
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(bootstrap.urllib.request, 'urlopen', lambda req, timeout=120: Stub())
    link = bootstrap.OFFICIAL_DOWNLOAD_PREFIX + 'b12241/demo.zip'
    target = tmp_path / 'pkg.zip'
    bootstrap.download(link, target, sha256_digest='sha256:' + hashlib.sha256(payload).hexdigest())
    assert target.read_bytes() == payload
    target.unlink()
    with pytest.raises(RuntimeError, match='SHA256'):
        bootstrap.download(link, target, sha256_digest='sha256:' + '0'*64)
    assert not target.exists()
    assert not (tmp_path / 'pkg.zip.partial').exists()

# Windows WinError 5 regression: never rename an extracted directory into a
# potentially locked existing llama.cpp directory. Publish in a unique folder.
def test_windows_locked_legacy_directory_does_not_block_new_runtime(bootstrap, monkeypatch, tmp_path):
    import zipfile
    old_dir = tmp_path / 'llama.cpp'
    old_dir.mkdir()
    (old_dir / 'locked-runtime.dll').write_bytes(b'unrelated existing file')
    monkeypatch.delenv('SURYA_LLAMA_SERVER_EXE', raising=False)
    monkeypatch.setattr(bootstrap, 'select_official_download', lambda _backend: (
        'https://github.com/ggml-org/llama.cpp/releases/download/b12242/llama-b12242-bin-win-vulkan-x64.zip',
        'llama-b12242-bin-win-vulkan-x64.zip', ''))

    def fake_download(_url, target, *, sha256_digest=''):
        with zipfile.ZipFile(target, 'w') as z:
            z.writestr('bin/llama-server.exe', b'MZ' + b'Z' * 2048)
            z.writestr('bin/ggml-vulkan.dll', b'VULKAN')
    monkeypatch.setattr(bootstrap, 'download', fake_download)
    real_replace = bootstrap.os.replace

    def deny_windows_directory_replace(source, target):
        if Path(source).is_dir():
            raise PermissionError(5, 'Access is denied', str(target))
        return real_replace(source, target)

    monkeypatch.setattr(bootstrap.os, 'replace', deny_windows_directory_replace)
    binary = bootstrap.install_server(tmp_path, 'vulkan')
    assert binary.is_file()
    assert binary.parent.parent.name == bootstrap.RUNTIME_ROOT
    assert (binary.parent / 'ggml-vulkan.dll').is_file()
    assert (old_dir / 'locked-runtime.dll').read_bytes() == b'unrelated existing file'
    assert (binary.parent / bootstrap.RUNTIME_MARKER).is_file()

    # Restart never re-downloads the ZIP or attempts directory replacement.
    monkeypatch.setattr(bootstrap, 'select_official_download', lambda _: (_ for _ in ()).throw(AssertionError('Unexpected network request')))
    assert bootstrap.install_server(tmp_path, 'vulkan') == binary


def test_incomplete_staged_runtime_not_selected_on_restart(bootstrap, monkeypatch, tmp_path):
    runtimes = tmp_path / bootstrap.RUNTIME_ROOT
    incomplete = runtimes / 'vulkan-something'
    incomplete.mkdir(parents=True)
    (incomplete / 'llama-server.exe').write_bytes(b'MZ' + b'X'*2000)
    assert bootstrap.cached_runtime(tmp_path, 'vulkan') is None
    (incomplete / bootstrap.RUNTIME_MARKER).write_text('{"backend":"cpu","server":"llama-server.exe"}', encoding='utf-8')
    assert bootstrap.cached_runtime(tmp_path, 'vulkan') is None


def test_result_file_reports_nonlegacy_runtime_path(bootstrap, monkeypatch, tmp_path):
    import sys, json
    model = tmp_path / 'models' / 'surya-2.5.gguf'
    projection = tmp_path / 'models' / 'surya-2.5-mmproj.gguf'
    server = tmp_path / 'llama-runtimes' / 'with spaces' / 'llama-server.exe'
    server.parent.mkdir(parents=True)
    server.write_bytes(b'MZ'+b'A'*2000)
    monkeypatch.setattr(bootstrap, 'install_weights', lambda _cache: (model, projection))
    monkeypatch.setattr(bootstrap, 'install_server', lambda _cache, _backend: server)
    result = tmp_path / 'bootstrap-result.json'
    monkeypatch.setattr(sys, 'argv', ['surya_host_bootstrap.py', '--cache', str(tmp_path), '--result-file', str(result)])
    bootstrap.main()
    assert json.loads(result.read_text(encoding='utf-8'))['server'] == str(server)


def test_powershell_uses_installer_result_for_executable():
    manager = (ROOT / 'scripts/ensure_host_surya.ps1').read_text()
    assert "'--result-file', $resultFile" in manager
    assert '$server = [string]$resolved.server' in manager
    assert "Join-Path $cache 'llama.cpp\\llama-server.exe'" not in manager


def test_app_control_block_runs_surya_in_docker():
    """V1.31.10: Windows App Control blocking llama-server.exe switches to the llama.cpp Docker image."""
    manager = (ROOT / 'scripts/ensure_host_surya.ps1').read_text()
    assert 'function Test-AppControlBlock' in manager
    assert "Save-Runtime -Runtime 'docker'" in manager
    assert 'ghcr.io/ggml-org/llama.cpp:server-cuda' in manager and "'ghcr.io/ggml-org/llama.cpp:server'" in manager
    assert "$bootstrapArgs += '--model-only'" in manager
    startup = (ROOT / 'script_docker/start_docker.ps1').read_text()
    assert 'Polaron starts WITHOUT OCR' in startup
    assert "throw \"Host SuryaOCR provisioning failed; preserving running forensic services. $suryaError\"" in startup
