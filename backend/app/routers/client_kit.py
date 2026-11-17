"""V1.31.8: keep each examiner computer's Polaron client files equal to the server's.

The workstation helper (host-drive-helper.ps1), the phone collection scripts and the Python modules they run
live on every examiner computer. Before, they changed only when someone copied a release onto that computer by
hand, so a client could keep running an old helper (for example 5.60, which kept showing an unplugged iPhone)
long after the server was updated.

Now the server is the source:
  GET  /api/client-kit/info       (signed-in user) the helper version the server has, for the page to compare.
  GET  /api/client-kit/setup.cmd  (signed-in user) a one-time setup file for this computer. It downloads the
                                  client files from this server, restarts the helper (never during a
                                  collection) and keeps the computer updated from then on.
  GET  /api/client-kit/manifest   (kit key) every client file with its SHA-256.
  GET  /api/client-kit/file       (kit key) one client file, only paths listed in the manifest.

The kit key is derived from the server secret. It only allows reading these product files (the same files
the release ZIP carries); it gives no access to cases, evidence or users.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response

from app.config import get_settings
from app.deps import CurrentUser, require_firm_permission
from app.service_identity import current_service

router = APIRouter(prefix="/api/client-kit", tags=["client-kit"])

KIT_KEY_HEADER = "X-Polaron-Kit-Key"
SETUP_FILENAME = "Polaron-Setup-This-Computer.cmd"
UPDATER_PATH = "scripts/polaron-client-update.ps1"
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::[0-9]{1,5})?$")
_SCRIPT_SUFFIXES = {".ps1", ".psm1", ".py", ".cmd"}
_HELPER_VERSION_RE = re.compile(r'\$script:HelperVersion\s*=\s*"([0-9.]+)"')

_cache_lock = threading.Lock()
_hash_cache: dict[str, tuple[int, int, str]] = {}


def _backend_dir() -> Path:
    # app/routers/client_kit.py -> backend (in the containers: /app).
    return Path(__file__).resolve().parents[2]


def _scripts_dir() -> Path:
    override = os.environ.get("POLARON_CLIENT_KIT_SCRIPTS_DIR", "").strip()
    if override:
        return Path(override)
    mounted = Path("/scripts")
    if (mounted / "host-drive-helper.ps1").is_file():
        return mounted
    return _backend_dir().parent / "scripts"


def kit_key() -> str:
    secret = (get_settings().jwt_secret or "").encode("utf-8")
    return hmac.new(secret, b"polaron-client-kit-v1", hashlib.sha256).hexdigest()[:48]


def _require_kit_key(value: str | None) -> None:
    if not value or not hmac.compare_digest(value.strip(), kit_key()):
        raise HTTPException(status_code=401, detail={"error": {"code": "kit_key", "message": "Client kit key required"}})


def _kit_files() -> list[tuple[str, Path]]:
    """(kit path, file on this server). Kit paths use forward slashes, relative to the Polaron folder."""
    out: list[tuple[str, Path]] = []
    scripts = _scripts_dir()
    if scripts.is_dir():
        for f in sorted(scripts.iterdir()):
            # Top-level client scripts; leading "_" marks one-off server diagnostics.
            if f.is_file() and f.suffix.lower() in _SCRIPT_SUFFIXES and not f.name.startswith("_"):
                out.append((f"scripts/{f.name}", f))
        agent = scripts / "client-evidence-agent"
        if agent.is_dir():
            for f in sorted(agent.iterdir()):
                if f.is_file() and f.suffix.lower() in _SCRIPT_SUFFIXES:
                    out.append((f"scripts/client-evidence-agent/{f.name}", f))
    app_dir = _backend_dir() / "app"
    if app_dir.is_dir():
        # Collections on the examiner computer run Python from backend/app (readable copies, Android backup).
        for f in sorted(app_dir.rglob("*.py")):
            if "__pycache__" in f.parts or not f.is_file():
                continue
            out.append(("backend/app/" + f.relative_to(app_dir).as_posix(), f))
    return out


def _sha256(path: Path) -> str:
    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _hash_cache.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    with _cache_lock:
        _hash_cache[key] = (st.st_size, st.st_mtime_ns, digest)
    return digest


def helper_version() -> str:
    try:
        text = (_scripts_dir() / "host-drive-helper.ps1").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = _HELPER_VERSION_RE.search(text)
    return m.group(1) if m else ""


def build_manifest() -> dict:
    files = []
    for kit_path, f in _kit_files():
        try:
            files.append({"path": kit_path, "sha256": _sha256(f), "size": f.stat().st_size})
        except OSError:
            continue
    digest = hashlib.sha256("\n".join(f"{x['path']} {x['sha256']}" for x in files).encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "kit_version": digest[:16],
        "helper_version": helper_version(),
        "files": files,
        "file_count": len(files),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/info")
def client_kit_info(current: CurrentUser = Depends(require_firm_permission("job:read"))):
    return {"ok": True, "helper_version": helper_version(), "setup_file": SETUP_FILENAME}


@router.get("/manifest")
def client_kit_manifest(key: str | None = Header(default=None, alias=KIT_KEY_HEADER)):
    _require_kit_key(key)
    return build_manifest()


@router.get("/file")
def client_kit_file(
    path: str = Query(..., max_length=300),
    key: str | None = Header(default=None, alias=KIT_KEY_HEADER),
):
    _require_kit_key(key)
    for kit_path, f in _kit_files():
        if kit_path == path:
            data = f.read_bytes()
            return Response(
                content=data,
                media_type="application/octet-stream",
                headers={"X-Polaron-Sha256": hashlib.sha256(data).hexdigest(), "Cache-Control": "no-store"},
            )
    raise HTTPException(status_code=404, detail={"error": {"code": "not_in_kit", "message": "Not a client kit file"}})


def setup_cmd_text(origin: str, service: str, key: str, generated_utc: str) -> str:
    """The setup file: a .cmd that runs the PowerShell below it. Only validated values are filled in."""
    if not _ORIGIN_RE.match(origin):
        raise ValueError("origin")
    if not re.fullmatch(r"[a-z\-]{1,40}", service) or not re.fullmatch(r"[0-9a-f]{16,128}", key):
        raise ValueError("service/key")
    lines = [
        "@echo off",
        "rem Polaron: set up this computer for phone collection, or update it to match the server.",
        f"rem Server: {origin}   Created: {generated_utc}",
        "rem It downloads the Polaron client files from that server, restarts the Polaron helper (never while a",
        "rem collection is running) and keeps this computer updated from then on. Run it as the examiner.",
        "setlocal",
        'set "POLARON_SETUP_FILE=%~f0"',
        'set "PS=%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"',
        '"%PS%" -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:POLARON_SETUP_FILE);'
        " $i=$t.IndexOf('#POLARON-PS' + '-BEGIN'); $p=Join-Path $env:TEMP 'polaron-setup.ps1';"
        ' [IO.File]::WriteAllText($p, $t.Substring($i)); & $p; exit $LASTEXITCODE"',
        "echo.",
        "pause",
        "exit /b",
        "#POLARON-PS-BEGIN",
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        f"$Server = '{origin}'",
        f"$Service = '{service}'",
        f"$KitKey = '{key}'",
        "try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}",
        "$headers = @{ 'X-Polaron-Kit-Key' = $KitKey; 'X-Aetheris-Service' = $Service }",
        "Write-Host ('Polaron setup: contacting ' + $Server + ' ...')",
        "try {",
        "    $manifest = Invoke-RestMethod -UseBasicParsing -Uri ($Server + '/api/client-kit/manifest') -Headers $headers -TimeoutSec 60",
        "} catch {",
        "    Write-Host ('Could not reach the Polaron server: ' + $_.Exception.Message) -ForegroundColor Red",
        "    Write-Host 'If Windows does not trust the server certificate, open Polaron by its name (not its IP address), download this file again and rerun it.'",
        "    exit 2",
        "}",
        f"$entry = @($manifest.files | Where-Object {{ $_.path -eq '{UPDATER_PATH}' }}) | Select-Object -First 1",
        "if (-not $entry) { Write-Host 'The server has no client updater; update the server first.' -ForegroundColor Red; exit 2 }",
        "$updater = Join-Path $env:TEMP 'polaron-client-update.ps1'",
        f"Invoke-WebRequest -UseBasicParsing -Uri ($Server + '/api/client-kit/file?path=' + [uri]::EscapeDataString('{UPDATER_PATH}')) -Headers $headers -OutFile $updater -TimeoutSec 120",
        "$got = (Get-FileHash -LiteralPath $updater -Algorithm SHA256).Hash.ToLowerInvariant()",
        "if ($got -ne ([string]$entry.sha256).ToLowerInvariant()) { Write-Host 'The downloaded updater did not match the server list; nothing was changed.' -ForegroundColor Red; exit 3 }",
        "& $updater -Server $Server -Service $Service -KitKey $KitKey -Setup",
        "exit $LASTEXITCODE",
        "",
    ]
    return "\r\n".join(lines)


@router.get("/setup.cmd")
def client_kit_setup(
    origin: str = Query(..., max_length=200),
    current: CurrentUser = Depends(require_firm_permission("job:read")),
):
    origin = origin.strip().rstrip("/")
    if not _ORIGIN_RE.match(origin):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "origin", "message": "Use the Polaron address shown in the browser (https://name[:port])"}},
        )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = setup_cmd_text(origin, current_service(), kit_key(), now)
    return Response(
        content=text.encode("ascii"),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{SETUP_FILENAME}"',
            "Cache-Control": "no-store",
        },
    )
