#!/usr/bin/env python3
"""Provision host-native Surya GGUF weights and a trusted Windows llama.cpp server.

The GitHub /releases/latest endpoint can point at a stable release with *no*
Windows executable assets. Prefer the newest numbered llama.cpp build release
that actually includes the requested Windows x64 backend; do not download a
source-only release. The official pinned build is a last-resort fallback.
Existing downloaded GGUF files and working llama-server.exe are never re-fetched.
Windows may deny replacing an existing directory (WinError 5); runtimes are
published in independent, versioned directories without directory renames.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile

REPO = 'datalab-to/surya-ocr-2.5-gguf'
FILES = ('surya-2.5.gguf', 'surya-2.5-mmproj.gguf')
RELEASES_API = 'https://api.github.com/repos/ggml-org/llama.cpp/releases'
OFFICIAL_DOWNLOAD_PREFIX = 'https://github.com/ggml-org/llama.cpp/releases/download/'
# Known official Windows x64 release with both CPU and Vulkan binaries.
# Used only when the GitHub API is blocked/rate-limited or has incomplete assets.
FALLBACK_BUILD = 'b12241'
ALLOWED_ARCHIVE_SUFFIXES = {'.exe', '.dll', '.json', '.txt', '.spv', '.dat'}
MAX_ARCHIVE_BYTES = 1_000_000_000
MAX_UNPACKED_BYTES = 2_000_000_000
MAX_ARCHIVE_FILES = 600


def get_json(url: str):
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Polaron-Surya-Setup/1.1',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        },
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        return json.load(response)


def choose_asset(assets, preference: str):
    """Return (official asset URL, ZIP name) for the requested Windows backend.

    This function is deliberately strict: a CPU archive must not be selected
    for Vulkan startup, where the default --n-gpu-layers would be misleading.
    """
    if preference not in {'vulkan', 'cpu'}:
        raise ValueError(f'Unsupported llama.cpp backend: {preference}')
    for asset in assets:
        name = str(asset.get('name', '')).lower()
        url = str(asset.get('browser_download_url', ''))
        # Match actual official names: llama-b12241-bin-win-vulkan-x64.zip
        # and llama-b12241-bin-win-cpu-x64.zip.
        if re.fullmatch(r'llama-[a-z0-9.]+-bin-win-' + preference + r'-x64\.zip', name) and url:
            return url, name
    raise RuntimeError(
        f'No Windows x64 llama.cpp {preference} release ZIP found. '
        'Set SURYA_LLAMA_SERVER_EXE to an existing llama-server.exe if required.'
    )


def choose_release_asset(releases, preference: str):
    """Skip source-only stable releases and choose an official binary-bearing build."""
    if not isinstance(releases, list):
        raise ValueError('GitHub releases API response must be a list')
    for release in releases:
        if not isinstance(release, dict) or release.get('draft'):
            continue
        try:
            url, name = choose_asset(release.get('assets') or [], preference)
        except RuntimeError:
            continue
        if not url.startswith(OFFICIAL_DOWNLOAD_PREFIX):
            continue
        asset = next(a for a in release['assets'] if str(a.get('name', '')).lower() == name)
        return url, name, str(asset.get('digest') or '')
    return None


def select_official_download(preference: str):
    # A manually chosen official numbered build can be pinned for repeatable
    # production installations, without pinning to the source-only latest tag.
    pin = os.environ.get('SURYA_LLAMA_BUILD_TAG', '').strip()
    if pin and not re.fullmatch(r'b[0-9]+', pin):
        raise ValueError('SURYA_LLAMA_BUILD_TAG must have format b12345')

    if pin:
        endpoints = [f'{RELEASES_API}/tags/{pin}']
    else:
        endpoints = [f'{RELEASES_API}?per_page=30&page={page}' for page in (1, 2)]

    for endpoint in endpoints:
        try:
            releases = get_json(endpoint)
            if isinstance(releases, dict):
                releases = [releases]
            selected = choose_release_asset(releases, preference)
            if selected:
                print(f'[Surya] Windows llama.cpp build selected: {selected[1]}', flush=True)
                return selected
        except Exception as exc:
            print(f'[Surya] GitHub release discovery unavailable ({exc}); checking fallback.', flush=True)
            break

    fallback = pin or FALLBACK_BUILD
    name = f'llama-{fallback}-bin-win-{preference}-x64.zip'
    url = OFFICIAL_DOWNLOAD_PREFIX + fallback + '/' + name
    print(f'[Surya] Using official fallback Windows build: {name}', flush=True)
    return url, name, ''


def download(url: str, target: Path, *, sha256_digest: str = ''):
    if not url.startswith(OFFICIAL_DOWNLOAD_PREFIX):
        raise ValueError('Refusing download outside official ggml-org llama.cpp release assets')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.partial')
    temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={'User-Agent': 'Polaron-Surya-Setup/1.1'})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_ARCHIVE_BYTES:
                    raise RuntimeError('llama.cpp release archive exceeds configured safe download limit')
                digest.update(block)
                output.write(block)
        if total < 1024:
            raise RuntimeError(f'llama.cpp ZIP download was unexpectedly small ({total} bytes)')
        expected = sha256_digest.removeprefix('sha256:').lower()
        if expected and digest.hexdigest() != expected:
            raise RuntimeError('llama.cpp archive SHA256 differs from GitHub release digest')
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def unpack_windows_server(archive: Path, destination: Path):
    """Stage only executables and required runtime libraries; no path traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    extracted = set()
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError('llama.cpp ZIP has an unexpected number of files')
        if bundle.testzip() is not None:
            raise RuntimeError('llama.cpp ZIP failed archive CRC verification')
        for member in members:
            path = member.filename.replace('\\', '/')
            parts = path.split('/')
            if member.is_dir() or path.startswith('/') or '..' in parts or ':' in path:
                continue
            basename = parts[-1]
            if not basename or Path(basename).suffix.lower() not in ALLOWED_ARCHIVE_SUFFIXES:
                continue
            if basename.casefold() in extracted:
                # Multiple readmes/metadata may occur in different subfolders.
                # Runtime binaries must not silently overwrite one another.
                if Path(basename).suffix.lower() in {'.exe', '.dll'}:
                    raise RuntimeError(f'Duplicate Windows runtime filename inside ZIP: {basename}')
                continue
            # The release is official, but still bound total extracted size.
            total += member.file_size
            if total > MAX_UNPACKED_BYTES:
                raise RuntimeError('llama.cpp ZIP exceeds safe uncompressed size')
            extracted.add(basename.casefold())
            with bundle.open(member) as source, (destination / basename).open('wb') as output:
                shutil.copyfileobj(source, output)
    binary = destination / 'llama-server.exe'
    if not binary.is_file() or binary.stat().st_size < 1024:
        raise RuntimeError('Downloaded llama.cpp archive has no usable llama-server.exe')
    return binary


RUNTIME_ROOT = 'llama-runtimes'
RUNTIME_MARKER = '.polaron-runtime.json'


def _valid_binary(binary: Path) -> bool:
    """A basic post-extraction check (not a substitute for Windows launch tests)."""
    try:
        return binary.is_file() and binary.stat().st_size >= 1024
    except OSError:
        return False


def cached_runtime(base: Path, backend: str) -> Path | None:
    """Find completed installs; ignore interrupted staging and other backends."""
    # Older Polaron versions put the binary directly in llama.cpp. Preserve it.
    legacy = base / 'llama.cpp' / 'llama-server.exe'
    if _valid_binary(legacy):
        print(f'[Surya] Reusing legacy cached llama.cpp server: {legacy}', flush=True)
        return legacy

    installs = base / RUNTIME_ROOT
    if not installs.is_dir():
        return None
    try:
        candidates = sorted(installs.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        marker = candidate / RUNTIME_MARKER
        binary = candidate / 'llama-server.exe'
        if not marker.is_file() or not _valid_binary(binary):
            continue
        try:
            info = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if info.get('backend') == backend and info.get('server') == 'llama-server.exe':
            print(f'[Surya] Reusing verified cached llama.cpp server: {binary}', flush=True)
            return binary
    return None


def install_server(base: Path, backend: str):
    override = os.environ.get('SURYA_LLAMA_SERVER_EXE', '').strip().strip('"')
    if override:
        binary = Path(override)
        if not _valid_binary(binary):
            raise FileNotFoundError(f'SURYA_LLAMA_SERVER_EXE is missing or unusable: {binary}')
        print(f'[Surya] Reusing administrator-provided llama.cpp server: {binary}', flush=True)
        return binary

    existing = cached_runtime(base, backend)
    if existing is not None:
        return existing

    url, name, release_digest = select_official_download(backend)
    print(f'[Surya] Downloading official Windows llama.cpp package: {name}', flush=True)
    base.mkdir(parents=True, exist_ok=True)
    runtime_root = base / RUNTIME_ROOT
    runtime_root.mkdir(parents=True, exist_ok=True)

    # IMPORTANT: Do not os.replace(...) or shutil.rmtree(...) the old
    # llama.cpp directory. Windows Defender and open DLL handles can make a
    # directory rename fail with WinError 5, even if extraction succeeded.
    # Unpack straight into an isolated, durable location. A manifest written
    # LAST is the completion marker; interrupted directories are ignored.
    runtime = Path(tempfile.mkdtemp(prefix=f'{backend}-{name.removesuffix(".zip")}-', dir=str(runtime_root)))
    try:
        # Keep the ZIP inside the new runtime folder so no unrelated temporary
        # directory needs to be renamed or deleted before launch on Windows.
        archive = runtime / 'release.zip'
        download(url, archive, sha256_digest=release_digest)
        binary = unpack_windows_server(archive, runtime)
        try:
            archive.unlink()
        except OSError:
            # Some antivirus products briefly hold the ZIP open after read.
            # This is harmless: only the validated executable is launched.
            print(f'[Surya] Download archive is temporarily locked; retained at {archive}', flush=True)
        metadata = {
            'format': 1,
            'backend': backend,
            'archive': name,
            'source': url,
            'server': binary.name,
        }
        (runtime / RUNTIME_MARKER).write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    except Exception as exc:
        # Best-effort cleanup only of our new, incomplete folder. A locked
        # executable must never prevent reporting the original exception.
        try:
            shutil.rmtree(runtime)
        except OSError:
            print(f'[Surya] Incomplete runtime left for later cleanup: {runtime}', flush=True)
        raise RuntimeError(
            f'Windows llama.cpp package install failed: {exc}. '
            'The previous runtime and all Surya model weights are preserved. '
            'If Windows Security quarantined the binary, inspect Protection history; '
            'otherwise set SURYA_LLAMA_SERVER_EXE to a trusted installed llama-server.exe.'
        ) from exc

    print(f'[Surya] Installed llama.cpp server without directory replacement: {binary}', flush=True)
    return binary


def install_weights(base: Path):
    from huggingface_hub import hf_hub_download
    folder = base / 'models'
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for filename in FILES:
        path = folder / filename
        if not path.is_file() or path.stat().st_size < 1024:
            print(f'[Surya] Downloading {REPO}/{filename} to persistent cache', flush=True)
            fetched = Path(hf_hub_download(repo_id=REPO, filename=filename, local_dir=str(folder)))
            if fetched != path:
                raise RuntimeError(f'Unexpected Surya model file path: {fetched}')
        else:
            print(f'[Surya] Reusing existing model weight: {filename}', flush=True)
        paths.append(path)
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cache', required=True)
    ap.add_argument('--backend', choices=['vulkan', 'cpu'], default='vulkan')
    ap.add_argument('--model-only', action='store_true')
    ap.add_argument('--result-file', default='', help='Optional JSON result path for PowerShell launchers')
    args = ap.parse_args()
    base = Path(args.cache).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    model, mmproj = install_weights(base)
    server = None if args.model_only else install_server(base, args.backend)
    result = {'model': str(model), 'mmproj': str(mmproj), 'server': str(server) if server else ''}
    if args.result_file:
        status = Path(args.result_file)
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'[Surya ERROR] {exc}', file=sys.stderr)
        sys.exit(2)
