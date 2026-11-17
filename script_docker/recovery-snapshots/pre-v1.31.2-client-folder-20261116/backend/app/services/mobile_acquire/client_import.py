"""V1.27.0 — copy a mobile image created on a client PC into the forensic server's case folder.

When the phone is plugged into the examiner's own PC (not the forensic server), the
browser asks the HostDrive helper *on that PC* to collect it into
``C:\\evidence\\<CASE>\\…`` (same §4.2 layout as the server). This module then makes
the server copy "as usual":

    browser ── Client Evidence Agent ──► read-only share of C:\\evidence\\<CASE>
    browser ── POST /api/acquisition/client-import ──► start_import()
    worker thread:
        mount the share read-only (FUSE; office LAN or the agent's outbound connection)
        stream every file of the selected run in chunks
            ──► server HostDrive helper  POST /acquisition/import/{chunk,commit}
                writes <root>\\evidence\\cases\\<CASE>\\… (the folder server-side
                collections use) and verifies SHA-256 *on the server disk* before a
                file gets its final name; an existing file is never overwritten
        seal 02_Original_Extraction/<run> read-only (§12)
        write provenance: 07_Logs/<run>/client_import_<id>.json
                          08_Hashes/<run>/client_import_<id>_sha256.txt
        release the share (revoked on the PC, mount removed)
        publish an acquisition result whose paths point at the server copy

All processing afterwards (registration, parsing, RAG, report) runs on the server from
that copy. The client copy in C:\\evidence is never modified.

Containers mount the evidence store read-only by design, which is why the server
HostDrive helper (the same process that writes server-side collections) does the
writing and the API only streams bytes to it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

import httpx

from app.services.client_evidence import ClientEvidenceError, is_valid_share_id

log = logging.getLogger("mobile_acquire.client_import")

SCHEMA = "polaron.client_mobile_import/1"
_KEY_PREFIX = "polaron:client-mobile-import"
STATE_TTL_SEC = 7 * 24 * 3600
TERMINAL = frozenset({"completed", "failed", "cancelled"})

RUN_FOLDERS_FULL: tuple[str, ...] = (
    "02_Original_Extraction",
    "03_Working_Copy",
    "05_Exports",
    "06_Reports",
    "07_Logs",
    "08_Hashes",
)
RUN_FOLDERS_PACKAGES: tuple[str, ...] = ("05_Exports", "06_Reports", "07_Logs", "08_Hashes")
SCOPES = {"full": RUN_FOLDERS_FULL, "packages": RUN_FOLDERS_PACKAGES}
PART_SUFFIX = ".polaron-part"

_SUMMARY_RE = re.compile(
    r"^(?:(?P<case>[^/]+)/)?(?P<logs>07_Logs|03_Logs_and_Hashes)/(?P<run>[^/]+)/collection_summary\.json$",
    re.IGNORECASE,
)
_ORIGINAL_RE = re.compile(r"^(?:(?P<case>[^/]+)/)?02_Original_Extraction/(?P<run>[^/]+)/", re.IGNORECASE)
_CASE_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_BAD_CHARS = set('<>:"|?*\\')


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return os.environ.get("CLIENT_MOBILE_IMPORT_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def workers() -> int:
    return _env_int("CLIENT_MOBILE_IMPORT_WORKERS", 4, 1, 16)


def chunk_bytes() -> int:
    return _env_int("CLIENT_MOBILE_IMPORT_CHUNK_MB", 16, 1, 64) * 1024 * 1024


def server_case_root() -> str:
    """Case root handed to the server helper; it maps /evidence/cases to <root>\\evidence\\cases."""
    return (os.environ.get("CLIENT_MOBILE_IMPORT_CASE_ROOT") or "/evidence/cases").strip() or "/evidence/cases"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", text or "").upper()


def safe_segment(seg: str) -> bool:
    """Mirror of the helper's SafeSegment(): what NTFS (\\\\?\\ form) can hold as one path part."""
    if not seg or seg in (".", "..") or len(seg) > 255:
        return False
    if any(ord(c) < 0x20 or c in _BAD_CHARS or c == "/" for c in seg):
        return False
    return not seg.lower().endswith(PART_SUFFIX)


def server_case_folder(name: str) -> str:
    """Folder name the server helper accepts; keeps the client's name whenever it can."""
    name = (name or "").strip()
    if _CASE_FOLDER_RE.match(name) and name.strip("."):
        return name
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._-")[:128]
    if not cleaned or not cleaned[0].isalnum():
        cleaned = ("CASE_" + cleaned).strip("_") or "CASE_UNKNOWN"
    return cleaned[:128]


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _win_join(base: str, *parts: str) -> str:
    out = base.rstrip("\\/")
    for p in parts:
        if p:
            out += "\\" + p.replace("/", "\\").strip("\\")
    return out


def _path_key(text: str) -> str:
    return str(text or "").replace("/", "\\").rstrip("\\").lower()


def rebase_paths(value: Any, old_prefix: str, new_prefix: str) -> Any:
    """Rewrite every string that lives under ``old_prefix`` (client case dir) to ``new_prefix``."""
    old_key = _path_key(old_prefix)
    if not old_key:
        return value

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            key = _path_key(node)
            if key == old_key:
                return new_prefix
            if key.startswith(old_key + "\\"):
                rest = node.replace("/", "\\")[len(old_prefix.rstrip("\\/")) + 1:]
                return _win_join(new_prefix, rest)
        return node

    return walk(value)


def server_paths(case_dir: str, run_name: str, scope: str) -> dict[str, str]:
    paths = {
        "case_root": case_dir,
        "run_name": run_name,
        "original": _win_join(case_dir, "02_Original_Extraction", run_name),
        "working": _win_join(case_dir, "03_Working_Copy", run_name),
        "logs": _win_join(case_dir, "07_Logs", run_name),
        "hashes": _win_join(case_dir, "08_Hashes", run_name),
        "exports": _win_join(case_dir, "05_Exports", run_name),
        "reports": _win_join(case_dir, "06_Reports", run_name),
        "authority": _win_join(case_dir, "01_Authority"),
    }
    if scope == "packages":
        # Only the evidence packages + logs/hashes were copied; analysis reads 05_Exports.
        paths["original"] = ""
        paths["working"] = ""
    return paths


# ---------------------------------------------------------------------------
# Run selection inside the client share
# ---------------------------------------------------------------------------

@dataclass
class ImportFile:
    share_rel: str      # path inside the client share (what the FUSE mount exposes)
    rel: str            # path inside the case folder (what the server helper writes)
    size: int
    mtime_ms: int


@dataclass
class RunSelection:
    case_prefix: str    # "" when the share root is the case folder, else "<CASE>"
    case_folder: str    # client case folder name
    run_name: str
    files: list[ImportFile]
    has_summary: bool
    summary_share_rel: str = ""


def _case_folder_from_root(client_root: str) -> str:
    return PurePosixPath(str(client_root or "").replace("\\", "/").rstrip("/")).name


def select_run(
    files: list[dict[str, Any]],
    *,
    client_root: str,
    case_id: str = "",
    run_name: str = "",
    scope: str = "full",
) -> RunSelection:
    """Pick the run to copy from a share manifest.

    The share may be the case folder (``C:\\evidence\\CASE``) or the cases root
    (``C:\\evidence``). Without an explicit run the newest sealed run wins.
    """
    if scope not in SCOPES:
        raise ClientEvidenceError(f"Unknown copy scope {scope!r}")
    wanted_run = (run_name or "").strip().lower()
    wanted_case = _norm(case_id)
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for item in files:
        path = str(item.get("path") or "")
        m = _SUMMARY_RE.match(path)
        if m:
            key = (m.group("case") or "", m.group("run"))
            cur = candidates.setdefault(key, {"mtime": 0, "summary": ""})
            cur["mtime"] = max(cur["mtime"], int(item.get("mtime_ms") or 0))
            cur["summary"] = path
            continue
        m = _ORIGINAL_RE.match(path)
        if m:
            key = (m.group("case") or "", m.group("run"))
            cur = candidates.setdefault(key, {"mtime": 0, "summary": ""})
            cur["mtime"] = max(cur["mtime"], int(item.get("mtime_ms") or 0))
    if not candidates:
        raise ClientEvidenceError(
            "No acquisition run was found in the selected folder. Select the case folder "
            "(for example C:\\evidence\\<CASE>) that contains 02_Original_Extraction and 07_Logs."
        )
    pool = list(candidates.items())
    if wanted_case:
        root_case = _norm(_case_folder_from_root(client_root))
        narrowed = [kv for kv in pool if _norm(kv[0][0] or root_case) == wanted_case]
        if narrowed:
            pool = narrowed
        elif any(kv[0][0] for kv in pool):
            raise ClientEvidenceError(f"Case {case_id} was not found in the selected folder.")
    if wanted_run:
        pool = [kv for kv in pool if kv[0][1].lower() == wanted_run]
        if not pool:
            raise ClientEvidenceError(f"Run {run_name} was not found in the selected case folder.")
    if len({kv[0][0] for kv in pool}) > 1 and not wanted_case:
        raise ClientEvidenceError(
            "The selected folder holds several cases. Select one case folder, or enter its case ID."
        )
    (prefix, run), meta = max(pool, key=lambda kv: (bool(kv[1]["summary"]), kv[1]["mtime"], kv[0][1]))
    folders = SCOPES[scope]
    lead = f"{prefix}/" if prefix else ""
    chosen: list[ImportFile] = []
    bad: list[str] = []
    for item in files:
        path = str(item.get("path") or "")
        if lead and not path.startswith(lead):
            continue
        rel = path[len(lead):]
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] not in folders or parts[1] != run:
            continue
        if not all(safe_segment(p) for p in parts):
            bad.append(rel)
            continue
        chosen.append(ImportFile(share_rel=path, rel=rel, size=int(item["size"]), mtime_ms=int(item.get("mtime_ms") or 0)))
    if bad:
        shown = ", ".join(bad[:3])
        raise ClientEvidenceError(
            f"{len(bad)} file name(s) in run {run} contain characters the forensic server cannot store ({shown}). "
            "Copy this run to the server manually and record the copy in the case notes."
        )
    if not chosen:
        raise ClientEvidenceError(f"Run {run} has no files to copy.")
    case_folder = prefix or _case_folder_from_root(client_root)
    return RunSelection(
        case_prefix=prefix,
        case_folder=case_folder,
        run_name=run,
        files=sorted(chosen, key=lambda f: (f.rel.split("/")[0] != "07_Logs", f.rel)),
        has_summary=bool(meta["summary"]),
        summary_share_rel=meta["summary"],
    )


# ---------------------------------------------------------------------------
# State (Redis, visible to every API worker)
# ---------------------------------------------------------------------------

def _redis():
    from app.services.client_evidence.registry import _redis as client_redis

    return client_redis()


def _state_key(import_id: str) -> str:
    return f"{_KEY_PREFIX}:{import_id}"


def _cancel_key(import_id: str) -> str:
    return f"{_KEY_PREFIX}:{import_id}:cancel"


def save_state(state: dict[str, Any]) -> None:
    state["updated_utc"] = _now()
    _redis().set(_state_key(state["import_id"]), json.dumps(state, default=str), ex=STATE_TTL_SEC)


def load_state(import_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-f]{20}", str(import_id or "")):
        return None
    raw = _redis().get(_state_key(import_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in state.items() if not k.startswith("_")}
    total = int(out.get("total_bytes") or 0)
    done = int(out.get("bytes_done") or 0)
    out["progress_pct"] = round(100.0 * done / total, 1) if total else (100.0 if out.get("status") == "completed" else 0.0)
    return out


def request_cancel(import_id: str) -> bool:
    state = load_state(import_id)
    if not state or state.get("status") in TERMINAL:
        return False
    _redis().set(_cancel_key(import_id), "1", ex=STATE_TTL_SEC)
    return True


def _cancelled(import_id: str) -> bool:
    try:
        return bool(_redis().get(_cancel_key(import_id)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Server HostDrive helper (the writer)
# ---------------------------------------------------------------------------

class HelperImportError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


class HelperWriter:
    """Thin client for the server helper's /acquisition/import/* endpoints."""

    def __init__(self, base_url: str, *, case_root: str, case_folder: str):
        from app.services.mobile_acquire import host_bridge

        self.base_url = base_url.rstrip("/")
        self.headers = {
            **host_bridge.helper_auth_headers(),
            "X-Import-Case-Root": case_root,
            "X-Import-Case-Id": case_folder,
            "User-Agent": "PolaronForensicServer/1.0 (client-mobile-import)",
        }
        self._client = httpx.Client(
            timeout=httpx.Timeout(300.0, connect=10.0),
            trust_env=False,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, *, content: bytes, headers: dict[str, str] | None = None, timeout: float | None = None) -> tuple[int, dict[str, Any]]:
        merged = {**self.headers, **(headers or {})}
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=10.0)
        try:
            resp = self._client.post(f"{self.base_url}{path}", content=content, headers=merged, **kwargs)
        except httpx.HTTPError as exc:
            raise HelperImportError(f"Forensic server helper did not answer ({type(exc).__name__})") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return resp.status_code, body

    def prepare(self, files: Iterable[ImportFile]) -> dict[str, Any]:
        lines = "".join(f"{f.size}\t{f.rel}\n" for f in files).encode("utf-8")
        status, body = self._post("/acquisition/import/prepare", content=lines, timeout=600.0)
        if status != 200 or not body.get("ok"):
            raise HelperImportError(
                str(body.get("detail") or body.get("error") or f"prepare failed (HTTP {status})"),
                status=status,
                body=body,
            )
        return body

    def chunk(self, rel: str, offset: int, data: bytes) -> tuple[int, dict[str, Any]]:
        return self._post(
            "/acquisition/import/chunk",
            content=data,
            headers={"X-Import-Rel": _b64(rel), "X-Import-Offset": str(offset), "Content-Type": "application/octet-stream"},
        )

    def commit(self, f: ImportFile, sha256: str) -> tuple[int, dict[str, Any]]:
        # The helper re-reads the whole part to hash it: allow time for multi-GB packages.
        timeout = max(300.0, f.size / (40 * 1024 * 1024))
        return self._post(
            "/acquisition/import/commit",
            content=b"",
            headers={
                "X-Import-Rel": _b64(f.rel),
                "X-Import-Size": str(f.size),
                "X-Import-Sha256": sha256,
                "X-Import-Mtime-Ms": str(f.mtime_ms),
            },
            timeout=timeout,
        )

    def seal(self, rels: list[str]) -> dict[str, Any]:
        status, body = self._post("/acquisition/import/seal", content="".join(r + "\n" for r in rels).encode("utf-8"), timeout=900.0)
        return body if status == 200 else {"ok": False, "error": body.get("detail") or f"HTTP {status}"}

    def abort(self, rels: list[str]) -> None:
        if not rels:
            return
        try:
            self._post("/acquisition/import/abort", content="".join(r + "\n" for r in rels).encode("utf-8"), timeout=120.0)
        except HelperImportError:
            log.warning("could not remove unverified parts after an interrupted import")


def helper_capabilities() -> dict[str, Any]:
    """What the server helper reports about itself (reachability, identity, import support)."""
    from app.services.mobile_acquire import host_bridge

    out: dict[str, Any] = {"reachable": False, "client_import": False, "computer_name": "", "helper_version": ""}
    try:
        if not host_bridge.helper_reachable():
            return out
        health = host_bridge.helper_health() or {}
    except Exception:
        return out
    out.update({
        "reachable": True,
        "client_import": bool(health.get("client_import")),
        "computer_name": str(health.get("computer_name") or ""),
        "helper_version": str(health.get("helper_version") or ""),
        "import_case_root": str(health.get("import_case_root") or ""),
        "root": str(health.get("root") or ""),
    })
    return out


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def start_import(
    *,
    share_id: str,
    token: str,
    port: int,
    addresses: list[str],
    hostname: str,
    client_root: str,
    file_count: int,
    total_bytes: int,
    manifest_sha256: str,
    agent_url: str | None,
    relay_supported: bool,
    skipped: int,
    agent_version: str,
    case_id: str,
    run_name: str,
    scope: str,
    request_ip: str | None,
    user_email: str,
    platform: str | None,
) -> dict[str, Any]:
    """Validate the client share, pick the run, and start the copy in the background."""
    from app.services.client_evidence import agent_client, registry, relay

    if not enabled():
        raise ClientEvidenceError("Copying client images to the server is disabled (CLIENT_MOBILE_IMPORT_ENABLED).")
    share_id = (share_id or "").strip().lower()
    if not is_valid_share_id(share_id):
        raise ClientEvidenceError("The agent returned an invalid share id")
    if len(token or "") < 24:
        raise ClientEvidenceError("The agent returned an invalid share token")
    scope = scope if scope in SCOPES else "full"

    caps = helper_capabilities()
    if not caps["reachable"]:
        raise ClientEvidenceError(
            "The HostDrive helper on the forensic server is not running, so the server copy cannot be written. "
            "Start it on the server (scripts\\ensure-host-drive-helper.ps1) and try again."
        )
    if not caps["client_import"]:
        raise ClientEvidenceError(
            f"The HostDrive helper on the forensic server (v{caps.get('helper_version') or '?'}) is older than "
            "this release and cannot store client images. Restart it on the server with "
            "scripts\\ensure-host-drive-helper.ps1 (needs v5.58 or later)."
        )

    import_id = uuid.uuid4().hex[:20]
    pseudo_job = f"acqimport-{import_id}"
    candidates, rejected = agent_client.build_candidates(
        port=port, explicit_url=agent_url, request_ip=request_ip, addresses=addresses, hostname=hostname,
    )
    use_relay = relay_supported and relay.enabled()
    if use_relay:
        candidates.append(relay.RELAY_URL)
    if not candidates:
        raise ClientEvidenceError(
            "None of this computer's addresses is permitted for client evidence"
            + (f" ({'; '.join(rejected)})" if rejected else "")
            + ". Install the latest Client Evidence Agent, which connects out to the server."
        )
    # The agent's outbound polls are accepted only for shares this server knows.
    registry.save_share(registry.ShareRecord(
        share_id=share_id, job_id=pseudo_job, token=token,
        agent_url=relay.RELAY_URL if use_relay else candidates[0],
        hostname=hostname, client_root=client_root, manifest_sha256=manifest_sha256, source_type="mobile",
    ))
    try:
        url, manifest = agent_client.choose_reachable(
            candidates, share_id=share_id, token=token, expected_digest=manifest_sha256 or None,
        )
        files = list(manifest["files"])
        if manifest.get("truncated"):
            raise ClientEvidenceError(
                "The selected folder lists more files than one share can hold, so a complete copy is not possible. "
                "Select the case folder of this collection only."
            )
        if file_count and len(files) != int(file_count):
            raise ClientEvidenceError("The agent manifest does not match the selected folder (file count differs)")
        root = str(manifest.get("root") or client_root or "")
        selection = select_run(files, client_root=root, case_id=case_id, run_name=run_name, scope=scope)
    except Exception:
        registry.delete_share(share_id, pseudo_job)
        raise

    record = registry.ShareRecord(
        share_id=share_id,
        job_id=pseudo_job,
        token=token,
        agent_url=url,
        base_urls=[u for u in candidates if u != url],
        hostname=str(manifest.get("hostname") or hostname or "")[:120],
        client_root=root[:1024],
        file_count=len(files),
        total_bytes=sum(int(f["size"]) for f in files),
        manifest_sha256=str(manifest["manifest_sha256"]),
        truncated=False,
        source_type="mobile",
        registered_at=time.time(),
        registered_by=user_email,
    )
    registry.save_share(record)

    client_case_dir = _win_join(root, selection.case_prefix) if selection.case_prefix else root.rstrip("\\/")
    state: dict[str, Any] = {
        "import_id": import_id,
        "status": "queued",
        "stage": "queued",
        "detail": "Waiting to start the copy to the forensic server",
        "scope": scope,
        "case_id": case_id or selection.case_folder,
        "case_folder": selection.case_folder,
        "server_case_folder": server_case_folder(selection.case_folder),
        "run_name": selection.run_name,
        "client_host": record.hostname,
        "client_case_dir": client_case_dir,
        "client_root": record.client_root,
        "transport": "relay" if relay.is_relay_url(url) else "direct",
        "share_id": share_id,
        "manifest_sha256": record.manifest_sha256,
        "agent_version": agent_version or str(manifest.get("agent_version") or ""),
        "skipped_on_client": int(skipped or 0),
        "total_files": len(selection.files),
        "total_bytes": sum(f.size for f in selection.files),
        "files_done": 0,
        "bytes_done": 0,
        "files_present": 0,
        "server_case_dir": "",
        "server_helper": {k: caps.get(k) for k in ("computer_name", "helper_version", "import_case_root")},
        "examiner": user_email,
        "platform": platform or "",
        "started_utc": _now(),
        "ended_utc": None,
        "warnings": [],
        "error": None,
        "result": None,
    }
    save_state(state)
    thread = threading.Thread(
        target=_run_import,
        args=(import_id, selection, pseudo_job),
        name=f"client-import-{import_id}",
        daemon=True,
    )
    thread.start()
    return public_state(state)


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

class _Cancelled(Exception):
    pass


class _Progress:
    """Counters shared by the copy threads; flushed to Redis at most once a second."""

    def __init__(self, state: dict[str, Any]):
        self.state = state
        self.lock = threading.Lock()
        self._last = 0.0

    def add(self, *, files: int = 0, nbytes: int = 0, present: int = 0, detail: str | None = None, force: bool = False) -> None:
        with self.lock:
            self.state["files_done"] = int(self.state.get("files_done") or 0) + files
            self.state["bytes_done"] = int(self.state.get("bytes_done") or 0) + nbytes
            self.state["files_present"] = int(self.state.get("files_present") or 0) + present
            if detail:
                self.state["detail"] = detail
            now = time.monotonic()
            if force or now - self._last >= 1.0:
                self._last = now
                try:
                    save_state(self.state)
                except Exception:
                    log.debug("progress flush failed", exc_info=True)


def _copy_one(
    writer: HelperWriter,
    mountpoint: Path,
    f: ImportFile,
    progress: _Progress,
    cancelled: Callable[[], bool],
    in_flight: set[str],
    in_flight_lock: threading.Lock,
) -> str:
    """Stream one file to the server helper; return the SHA-256 verified on the server disk."""
    step = chunk_bytes()
    last_error = ""
    for attempt in range(1, 4):
        if cancelled():
            raise _Cancelled()
        sent = 0
        digest = hashlib.sha256()
        with in_flight_lock:
            in_flight.add(f.rel)
        try:
            with open(mountpoint / PurePosixPath(f.share_rel), "rb", buffering=0) as src:
                offset = 0
                while True:
                    if cancelled():
                        raise _Cancelled()
                    data = src.read(step)
                    if not data and offset > 0:
                        break
                    digest.update(data)
                    status, body = writer.chunk(f.rel, offset, data)
                    if status == 409 and body.get("error") == "exists":
                        raise HelperImportError("exists", status=409, body=body)
                    if status != 200 or not body.get("ok"):
                        raise HelperImportError(str(body.get("detail") or body.get("error") or f"HTTP {status}"), status=status, body=body)
                    offset += len(data)
                    sent += len(data)
                    progress.add(nbytes=len(data))
                    if not data:
                        break  # empty file: one empty chunk creates the part
            if offset != f.size:
                raise OSError(f"{f.rel} is {offset} bytes on the client, the share listed {f.size}")
            sha = digest.hexdigest()
            status, body = writer.commit(f, sha)
            if status == 200 and body.get("ok"):
                if str(body.get("sha256") or "").lower() != sha:
                    raise HelperImportError("server reported a different SHA-256 after commit", status=500, body=body)
                with in_flight_lock:
                    in_flight.discard(f.rel)
                progress.add(files=1)
                return sha
            if status == 409 and body.get("error") == "conflict":
                raise ClientEvidenceError(
                    f"A different file already exists on the forensic server at {f.rel}. It was not overwritten. "
                    "Remove or rename that run folder on the server only if it is not evidence, then copy again."
                )
            last_error = str(body.get("error") or body.get("detail") or f"HTTP {status}")
            if status != 422:
                raise HelperImportError(last_error, status=status, body=body)
            log.warning("client import %s: server-side verification failed for %s (%s), attempt %d", f.rel, f.rel, last_error, attempt)
        except _Cancelled:
            raise
        except ClientEvidenceError:
            raise
        except HelperImportError as exc:
            if exc.status == 409 and exc.body.get("error") == "exists":
                with in_flight_lock:
                    in_flight.discard(f.rel)
                if int(exc.body.get("size") or -1) != f.size:
                    raise ClientEvidenceError(
                        f"A different file already exists on the forensic server at {f.rel}. It was not overwritten."
                    ) from exc
                # Same size, created by an earlier verified copy of this run (only commit makes final files).
                progress.add(files=1, present=1, nbytes=f.size - sent)
                return ""
            last_error = str(exc)
            log.warning("client import: %s failed on attempt %d: %s", f.rel, attempt, exc)
        except OSError as exc:
            # EIO from the read-only mount: the file changed on the client or the PC was
            # unreachable for longer than CLIENT_EVIDENCE_RETRY_SECONDS.
            last_error = f"reading {f.rel} from the client failed: {exc}"
            log.warning("client import: %s", last_error)
        progress.add(nbytes=-sent)
        time.sleep(min(30, 3 * attempt))
    raise ClientEvidenceError(f"Could not copy {f.rel} to the forensic server: {last_error}")


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _build_result(
    state: dict[str, Any],
    selection: RunSelection,
    summary: dict[str, Any] | None,
    case_dir: str,
    warnings: list[str],
) -> dict[str, Any]:
    from app.services.mobile_acquire.run_registry import _normalize_result, _result_file_count

    run = selection.run_name
    base = dict(summary or {})
    base = rebase_paths(base, state["client_case_dir"], case_dir)
    paths = dict(base.get("paths") or {})
    paths.update(server_paths(case_dir, run, state["scope"]))
    base["paths"] = paths
    pkg = dict(base.get("evidence_package") or {})
    pkg.setdefault("run_name", run)
    pkg.setdefault("extraction_data", [])
    pkg["collection_summary"] = _win_join(case_dir, "07_Logs", run, "collection_summary.json") if selection.has_summary else ""
    pkg["complete"] = True
    base["evidence_package"] = pkg
    base["run_name"] = run
    base["stage_reached"] = "complete"
    base["ok"] = bool(base.get("ok", True)) or _result_file_count(base) > 0
    base.setdefault("device_profile", base.get("device") or {})
    base["warnings"] = list(base.get("warnings") or []) + warnings
    base["client_import"] = {
        "import_id": state["import_id"],
        "client_host": state["client_host"],
        "client_case_dir": state["client_case_dir"],
        "server_case_dir": case_dir,
        "files": state["total_files"],
        "bytes": state["total_bytes"],
        "already_on_server": state.get("files_present", 0),
        "scope": state["scope"],
        "transport": state["transport"],
        "provenance": _win_join(case_dir, "07_Logs", run, f"client_import_{state['import_id']}.json"),
    }
    return _normalize_result(base)


def _put_small_file(writer: HelperWriter, rel: str, data: bytes) -> str:
    f = ImportFile(share_rel="", rel=rel, size=len(data), mtime_ms=int(time.time() * 1000))
    status, body = writer.chunk(rel, 0, data)
    if status != 200 or not body.get("ok"):
        raise HelperImportError(str(body.get("detail") or body.get("error") or f"HTTP {status}"), status=status, body=body)
    sha = hashlib.sha256(data).hexdigest()
    status, body = writer.commit(f, sha)
    if status != 200 or not body.get("ok"):
        raise HelperImportError(str(body.get("detail") or body.get("error") or f"HTTP {status}"), status=status, body=body)
    return sha


def _run_import(import_id: str, selection: RunSelection, pseudo_job: str) -> None:
    from app.services.client_evidence import mount
    from app.services.client_evidence.job_source import release_share
    from app.services.mobile_acquire import host_bridge

    state = load_state(import_id)
    if state is None:
        return
    progress = _Progress(state)
    writer: HelperWriter | None = None
    in_flight: set[str] = set()
    in_flight_lock = threading.Lock()
    hashes: dict[str, str] = {}
    warnings: list[str] = []

    def cancelled() -> bool:
        return _cancelled(import_id)

    def stage(name: str, detail: str) -> None:
        state["status"] = "running"
        state["stage"] = name
        state["detail"] = detail
        save_state(state)

    try:
        stage("connecting", f"Connecting to {state['client_host'] or 'the client computer'}")
        mp = mount.ensure_mounted(state["share_id"])

        summary = None
        if selection.summary_share_rel:
            summary = _read_json_file(mp / PurePosixPath(selection.summary_share_rel))
        family = ""
        if isinstance(summary, dict):
            device = summary.get("device") or summary.get("device_profile") or {}
            if isinstance(device, dict):
                family = str(device.get("os_family") or "").lower()
        platform = str(state.get("platform") or "")
        if platform and family and platform not in family:
            raise ClientEvidenceError(
                f"This collection is from a {family} device but this is the {platform.title()} backend. "
                f"Copy it from the {family.title()} acquisition page."
            )
        lead = f"{selection.case_prefix}/" if selection.case_prefix else ""
        marker = mp / PurePosixPath(f"{lead}05_Exports/{selection.run_name}/FULL_ZIP_LOCATION.txt")
        try:
            if marker.is_file():
                where = marker.read_text(encoding="utf-8-sig", errors="replace").strip().splitlines()[0][:400]
                warnings.append(
                    f"The full ZIP of this run was written outside the case folder on the client ({where}) because "
                    "the case drive was short of space. It was not copied; the other packages were."
                )
        except OSError:
            pass
        if int(state.get("skipped_on_client") or 0):
            warnings.append(
                f"{state['skipped_on_client']} item(s) in the client case folder are links or could not be read and "
                "were not copied."
            )

        writer = HelperWriter(host_bridge.helper_base_url(), case_root=server_case_root(), case_folder=state["server_case_folder"])
        stage("preparing", "Checking the forensic server case folder")
        prep = writer.prepare(selection.files)
        case_dir = str(prep.get("case_dir") or "")
        state["server_case_dir"] = case_dir
        conflicts = prep.get("conflicts") or []
        if conflicts:
            shown = ", ".join(str(c.get("rel")) for c in conflicts[:3])
            raise ClientEvidenceError(
                f"{len(conflicts)} file(s) of run {selection.run_name} already exist on the forensic server with "
                f"different content ({shown}). Nothing was overwritten."
            )
        present = {str(r) for r in (prep.get("existing") or [])}
        todo = [f for f in selection.files if f.rel not in present]
        if present:
            progress.add(files=len(present), present=len(present), nbytes=sum(f.size for f in selection.files if f.rel in present))
            warnings.append(
                f"{len(present)} file(s) were already on the forensic server from an earlier copy of this run and were kept."
            )
        stage("copying", f"Copying {len(todo)} file(s) from {state['client_host']} to the forensic server")

        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=workers(), thread_name_prefix=f"ci-{import_id[:6]}") as pool:
            futures = {
                pool.submit(_copy_one, writer, mp, f, progress, cancelled, in_flight, in_flight_lock): f
                for f in todo
            }
            for fut in as_completed(futures):
                f = futures[fut]
                try:
                    sha = fut.result()
                    if sha:
                        hashes[f.rel] = sha
                except BaseException as exc:  # noqa: BLE001 - first failure stops the copy
                    errors.append(exc)
                    _redis().set(_cancel_key(import_id), "1", ex=STATE_TTL_SEC)
                    for other in futures:
                        other.cancel()
                    break
        if errors:
            real = [e for e in errors if not isinstance(e, _Cancelled)]
            if real:
                raise real[0]
            raise _Cancelled()
        if cancelled():
            raise _Cancelled()

        stage("sealing", "Marking the server copy of the original extraction read-only")
        sealed = writer.seal([f.rel for f in selection.files if f.rel.startswith("02_Original_Extraction/")])
        if not sealed.get("ok") or int(sealed.get("failed") or 0):
            warnings.append(
                "Some files of the server copy of 02_Original_Extraction could not be marked read-only "
                f"({sealed.get('failed') or sealed.get('error')})."
            )

        stage("provenance", "Writing the copy record")
        run = selection.run_name
        hash_rel = f"08_Hashes/{run}/client_import_{import_id}_sha256.txt"
        hash_lines = "".join(f"{hashes[rel]}  {rel}\n" for rel in sorted(hashes))
        hash_sha = _put_small_file(writer, hash_rel, hash_lines.encode("utf-8"))
        state["ended_utc"] = _now()
        provenance = {
            "schema": SCHEMA,
            "import_id": import_id,
            "run_name": run,
            "scope": state["scope"],
            "examiner": state.get("examiner") or "",
            "started_utc": state["started_utc"],
            "completed_utc": state["ended_utc"],
            "client": {
                "hostname": state["client_host"],
                "case_dir": state["client_case_dir"],
                "share_root": state["client_root"],
                "share_id": state["share_id"],
                "manifest_sha256": state["manifest_sha256"],
                "agent_version": state.get("agent_version") or "",
                "transport": state["transport"],
            },
            "server": {
                "case_dir": case_dir,
                "case_folder": state["server_case_folder"],
                "helper": state.get("server_helper") or {},
                "paths": server_paths(case_dir, run, state["scope"]),
            },
            "files": {
                "count": state["total_files"],
                "bytes": state["total_bytes"],
                "copied_and_verified": len(hashes),
                "already_on_server": state.get("files_present", 0),
            },
            "verification": (
                "Each copied file was hashed (SHA-256) while it was read from the client's read-only share and "
                "again from the forensic server disk before it received its final name; both digests matched."
            ),
            "hash_list": hash_rel,
            "hash_list_sha256": hash_sha,
            "sealed": sealed,
            "warnings": warnings,
        }
        _put_small_file(
            writer,
            f"07_Logs/{run}/client_import_{import_id}.json",
            json.dumps(provenance, indent=2).encode("utf-8"),
        )

        result = _build_result(state, selection, summary if isinstance(summary, dict) else None, case_dir, warnings)
        state.update({
            "status": "completed",
            "stage": "complete",
            "detail": f"Server copy ready in {case_dir}",
            "warnings": warnings,
            "result": result,
            "run": {
                "run_id": "",
                "run_name": run,
                "case_id": state.get("case_id") or selection.case_folder,
                "evidence_id": str((summary or {}).get("evidence_id") or "") if isinstance(summary, dict) else "",
                "adapter": "ios_lockdown" if "ios" in family else "android_mtp" if "android" in family else "",
                "device_id": "",
                "examiner": state.get("examiner") or "",
                "status": "completed",
                "started_utc": state["started_utc"],
                "ended_utc": state["ended_utc"],
                "cancel_requested": False,
                "progress": {
                    "stage": "complete",
                    "item": "complete",
                    "detail": "Collected on the client and copied to the forensic server",
                    "category": "Complete",
                    "bytes_done": state["total_bytes"],
                    "bytes_total": state["total_bytes"],
                    "progress_pct": 100,
                    "files_seen": state["total_files"],
                    "case_path": case_dir,
                    "updated_utc": _now(),
                },
                "error": None,
                "result": result,
                "client_import": True,
            },
        })
        save_state(state)
        log.info("client import %s complete: %s files, %s bytes -> %s", import_id, state["total_files"], state["total_bytes"], case_dir)
    except _Cancelled:
        if writer is not None:
            writer.abort(sorted(in_flight))
        state.update({"status": "cancelled", "stage": "cancelled", "detail": "Copy stopped by the examiner", "ended_utc": _now()})
        state["warnings"] = warnings + [
            "Files already verified on the server were kept; copying the same run again continues from there."
        ]
        save_state(state)
    except Exception as exc:  # noqa: BLE001 - every failure must reach the UI
        if writer is not None:
            writer.abort(sorted(in_flight))
        log.warning("client import %s failed: %s", import_id, exc, exc_info=not isinstance(exc, (ClientEvidenceError, HelperImportError)))
        state.update({
            "status": "failed",
            "stage": "failed",
            "detail": "The copy to the forensic server did not finish",
            "error": str(exc),
            "warnings": warnings,
            "ended_utc": _now(),
        })
        save_state(state)
    finally:
        if writer is not None:
            writer.close()
        try:
            release_share(state["share_id"], job_id=pseudo_job, revoke=True)
        except Exception:
            log.warning("could not release client share %s", state.get("share_id"), exc_info=True)
        try:
            _redis().delete(_cancel_key(import_id))
        except Exception:
            pass


__all__ = [
    "ImportFile",
    "RunSelection",
    "HelperWriter",
    "enabled",
    "helper_capabilities",
    "load_state",
    "public_state",
    "rebase_paths",
    "request_cancel",
    "select_run",
    "server_case_folder",
    "server_paths",
    "start_import",
]
