"""V1.31.14 - a phone collection made on an examiner computer is sent to the forensic server while it runs.

Before, a collection on a client computer was written to C:\\evidence\\<CASE> there, and only afterwards
copied to the server (through the Client Evidence Agent or this browser). Now the server is the
destination from the start:

    page    POST /api/acquisition/client-stream             -> session id + upload token (this server only)
    page    POST 127.0.0.1:9876/acquisition/run?native=...  -> the collection on the client, with the session
    helper  polaron-stream-upload.ps1 (client)              -> sends finished files in parallel, and pieces of a large file together:
              PUT  /api/acquisition/client-stream/<id>/chunk    (<= 64 MiB; X-Stream-Piece: 1 allows any offset)
              POST /api/acquisition/client-stream/<id>/commit   (SHA-256 verified on the server disk)
            when the collection ends:
              POST /api/acquisition/client-stream/<id>/finish   (every file present + hashes match)
            then deletes the client copy (unless the examiner keeps one) and leaves a receipt
    page    GET  /api/acquisition/client-import/<id>         -> progress, then the server-path result

The bytes are written by the server HostDrive helper (/acquisition/import/*), the same writer the
client copy uses: a file gets its final name only after its SHA-256 was checked on the server disk,
and an existing file is never overwritten. The session state lives with the client copies, so the
page follows a stream exactly like a copy.

The client helper sends only to the server it was set up for (client-kit.json of the setup file);
the upload token is valid for one session, one case and one run.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import uuid
from typing import Any

from app.services.mobile_acquire.client_import import (
    SCOPES,
    STATE_TTL_SEC,
    TERMINAL,
    HelperImportError,
    HelperWriter,
    ImportFile,
    RunSelection,
    _build_result,
    _cancelled,
    _now,
    _put_small_file,
    _redis,
    _state_key,
    _win_join,
    enabled,
    helper_capabilities,
    load_state,
    public_state,
    safe_segment,
    save_state,
    server_case_folder,
    server_case_root,
    server_paths,
)

log = logging.getLogger("mobile_acquire.client_stream")

# Parallel commits each rewrite the session document. One lock per session keeps a slow flush from
# saving an older file count over a newer one. The lock is not held while bytes are written.
_state_locks_guard = threading.Lock()
_state_locks: dict[str, threading.Lock] = {}


def _state_lock(sid: str) -> threading.Lock:
    with _state_locks_guard:
        lock = _state_locks.get(sid)
        if lock is None:
            lock = threading.Lock()
            _state_locks[sid] = lock
        return lock

SCHEMA = "polaron.client_mobile_stream/1"
TOKEN_HEADER = "X-Polaron-Stream-Token"
MAX_CHUNK_BYTES = 64 * 1024 * 1024


class StreamError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def stream_chunk_bytes() -> int:
    """Chunk size the client uploader uses (scripts/polaron-stream-upload.ps1 -ChunkMB, default 8)."""
    return 8 * 1024 * 1024


def client_case_folder(case_id: str) -> str:
    """The folder the client collection writes (native_ios_job / native_mtp_job use the same rule)."""
    name = re.sub(r"[^\w\-.]", "_", (case_id or "").strip())
    return name or "CASE-UNKNOWN"


def _files_key(sid: str) -> str:
    return f"{_state_key(sid)}:files"


def _count_key(sid: str) -> str:
    return f"{_state_key(sid)}:count"


def _token_digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _writer(state: dict[str, Any]) -> HelperWriter:
    from app.services.mobile_acquire import host_bridge

    return HelperWriter(host_bridge.helper_base_url(), case_root=server_case_root(), case_folder=state["server_case_folder"])


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def create_session(
    *,
    case_id: str,
    scope: str = "full",
    keep_local: bool = False,
    client_host: str = "",
    examiner: str = "",
    platform: str | None = None,
    service: str = "",
) -> dict[str, Any]:
    if not enabled():
        raise StreamError(403, "disabled", "Sending client images to the server is disabled (CLIENT_MOBILE_IMPORT_ENABLED).")
    caps = helper_capabilities()
    if not caps["reachable"]:
        raise StreamError(
            503, "server_helper_offline",
            "The HostDrive helper on the forensic server is not running, so the server cannot receive the image. "
            "Start it on the server (scripts\\ensure-host-drive-helper.ps1) and try again.",
        )
    if not caps["client_import"]:
        raise StreamError(
            503, "server_helper_old",
            f"The HostDrive helper on the forensic server (v{caps.get('helper_version') or '?'}) cannot store client "
            "images. Restart it on the server with scripts\\ensure-host-drive-helper.ps1.",
        )
    scope = scope if scope in SCOPES else "full"
    sid = uuid.uuid4().hex[:20]
    token = secrets.token_urlsafe(32)
    folder = server_case_folder(client_case_folder(case_id))
    state: dict[str, Any] = {
        "import_id": sid,
        "kind": "stream",
        "status": "waiting",
        "stage": "waiting",
        "detail": "Waiting for the collection on the client computer to send its first files",
        "case_id": (case_id or "").strip(),
        "client_case_folder": client_case_folder(case_id),
        "server_case_folder": folder,
        "server_case_dir": "",
        "run_name": "",
        "scope": scope,
        "keep_local": bool(keep_local),
        "transport": "client_stream",
        "client_host": (client_host or "")[:120],
        "client_case_dir": "",
        "examiner": examiner,
        "platform": platform or "",
        "total_files": 0,
        "total_bytes": 0,
        "files_done": 0,
        "bytes_done": 0,
        "files_present": 0,
        "started_utc": _now(),
        "ended_utc": None,
        "warnings": [],
        "error": None,
        "result": None,
        "server_helper": caps,
        "_token_sha256": _token_digest(token),
    }
    writer = _writer(state)
    try:
        prep = writer.prepare([])  # creates the case skeleton on the server and names its folder
        state["server_case_dir"] = str(prep.get("case_dir") or "")
    except HelperImportError as exc:
        raise StreamError(503, "server_helper_error", f"The forensic server helper refused the case folder: {exc}") from exc
    finally:
        writer.close()
    save_state(state)
    return {
        "session_id": sid,
        "token": token,
        "chunk_bytes": stream_chunk_bytes(),
        "service": service,
        "state": public_state(state),
    }


def _session(sid: str, token: str, *, allow_terminal: bool = False) -> dict[str, Any]:
    state = load_state(sid)
    if not state or state.get("kind") != "stream":
        raise StreamError(404, "unknown_session", "This server does not know the upload session (it may have expired).")
    if not hmac.compare_digest(_token_digest(token), str(state.get("_token_sha256") or "")):
        raise StreamError(401, "unauthorized", "Wrong upload token for this session.")
    if _cancelled(sid) and state.get("status") not in TERMINAL:
        state.update({"status": "cancelled", "stage": "cancelled", "detail": "Stopped by the examiner", "ended_utc": _now()})
        save_state(state)
    status = str(state.get("status") or "")
    if status == "cancelled":
        raise StreamError(409, "cancelled", "The examiner stopped sending this collection to the server.")
    if status in TERMINAL and not allow_terminal:
        raise StreamError(409, status, f"This upload session is already {status}.")
    return state


def _rel_parts(state: dict[str, Any], rel: str) -> list[str]:
    rel = (rel or "").strip()
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] not in SCOPES[state["scope"]] or not all(safe_segment(p) for p in parts):
        raise StreamError(400, "bad_path", f"Not a file of this collection's run folders: {rel[:200]}")
    run = state.get("run_name") or ""
    if run and parts[1] != run:
        raise StreamError(409, "other_run", f"This session receives run {run}, not {parts[1]}.")
    return parts


def _claim_run(state: dict[str, Any], run: str, client_case_dir: str = "") -> None:
    sid = str(state.get("import_id") or "")
    with _state_lock(sid):
        fresh = load_state(sid) or state
        changed = False
        if not fresh.get("run_name"):
            fresh["run_name"] = run
            changed = True
        if client_case_dir and not fresh.get("client_case_dir"):
            fresh["client_case_dir"] = client_case_dir[:1024]
            changed = True
        if fresh.get("status") == "waiting":
            fresh.update({"status": "running", "stage": "receiving", "detail": "Receiving the collection from the client computer"})
            changed = True
        if changed:
            save_state(fresh)
        if fresh is not state:
            state.clear()
            state.update(fresh)


def decode_rel(header_value: str) -> str:
    text = (header_value or "").strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise StreamError(400, "bad_path", "X-Stream-Rel must be base64 of the UTF-8 path") from exc


def put_chunk(
    sid: str, token: str, rel: str, offset: int, data: bytes, client_case_dir: str = "", piece: bool = False
) -> tuple[int, dict[str, Any]]:
    state = _session(sid, token)
    parts = _rel_parts(state, rel)
    if len(data) > MAX_CHUNK_BYTES or offset < 0:
        raise StreamError(413, "bad_chunk", "Chunks are at most 64 MiB, at a non-negative offset.")
    _claim_run(state, parts[1], client_case_dir)
    writer = _writer(state)
    try:
        status, body = writer.chunk(rel, offset, data, piece=piece)
    except HelperImportError as exc:
        raise StreamError(503, "server_helper_error", str(exc)) from exc
    finally:
        writer.close()
    if status == 200 and body.get("ok"):
        return 200, {"ok": True, "size": int(body.get("size") or offset + len(data))}
    if status == 409:
        # exists (a verified copy is already on the server) or offset_mismatch (send from "size").
        return 409, {"ok": False, "error": str(body.get("error") or "conflict"), "size": int(body.get("size") or 0)}
    raise StreamError(502 if status >= 500 else status or 502, str(body.get("error") or "chunk_failed"),
                      str(body.get("detail") or body.get("error") or f"HTTP {status}"))


def _flush_progress(sid: str, totals: tuple[int, int] | None = None) -> None:
    with _state_lock(sid):
        _flush_progress_locked(sid, totals)


def _flush_progress_locked(sid: str, totals: tuple[int, int] | None = None) -> None:
    state = load_state(sid)
    if not state or state.get("status") in TERMINAL:
        return
    counts = {
        (k.decode() if isinstance(k, bytes) else str(k)): v
        for k, v in (_redis().hgetall(_count_key(sid)) or {}).items()
    }

    def num(key: str) -> int:
        try:
            return int(counts.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    state["files_done"] = num("files")
    state["bytes_done"] = num("bytes")
    state["files_present"] = num("present")
    if totals:
        state["total_files"] = max(int(state.get("total_files") or 0), int(totals[0]), state["files_done"])
        state["total_bytes"] = max(int(state.get("total_bytes") or 0), int(totals[1]), state["bytes_done"])
    state["detail"] = f"{state['files_done']} file(s) verified on the forensic server"
    save_state(state)


def commit_file(
    sid: str,
    token: str,
    *,
    rel: str,
    size: int,
    sha256: str,
    mtime_ms: int = 0,
    total_files: int = 0,
    total_bytes: int = 0,
) -> dict[str, Any]:
    state = _session(sid, token)
    parts = _rel_parts(state, rel)
    sha = (sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha) or size < 0:
        raise StreamError(400, "bad_commit", "A commit needs the file size and its SHA-256.")
    _claim_run(state, parts[1])
    writer = _writer(state)
    try:
        status, body = writer.commit(ImportFile(share_rel="", rel=rel, size=int(size), mtime_ms=int(mtime_ms or 0)), sha)
    except HelperImportError as exc:
        raise StreamError(503, "server_helper_error", str(exc)) from exc
    finally:
        writer.close()
    if status == 200 and body.get("ok"):
        server_sha = str(body.get("sha256") or "").lower()
        if server_sha != sha:
            raise StreamError(422, "hash_mismatch", "The server disk holds a different SHA-256 after the commit.")
        _record_commit(sid, rel, int(size), sha, bool(body.get("exists")))
        _flush_progress(sid, (int(total_files or 0), int(total_bytes or 0)))
        return {"ok": True, "sha256": server_sha, "path": str(body.get("path") or ""), "exists": bool(body.get("exists"))}
    if status == 409 and body.get("error") == "conflict":
        raise StreamError(
            409, "conflict",
            f"A different file already exists on the forensic server at {rel}. It was not overwritten.",
        )
    if status == 422:
        # The part on the server did not match: the client sends the file again from the start.
        raise StreamError(422, str(body.get("error") or "verify_failed"), "The server copy did not match; send the file again.")
    raise StreamError(502 if status >= 500 else status or 502, str(body.get("error") or "commit_failed"),
                      str(body.get("detail") or body.get("error") or f"HTTP {status}"))


def _record_commit(sid: str, rel: str, size: int, sha: str, present: bool) -> None:
    r = _redis()
    if r.hsetnx(_files_key(sid), rel, f"{int(size)} {sha}"):
        r.hincrby(_count_key(sid), "files", 1)
        r.hincrby(_count_key(sid), "bytes", int(size))
        if present:
            r.hincrby(_count_key(sid), "present", 1)
    r.expire(_files_key(sid), STATE_TTL_SEC)
    r.expire(_count_key(sid), STATE_TTL_SEC)


def _forget_commit(sid: str, rel: str, size: int) -> None:
    r = _redis()
    if r.hdel(_files_key(sid), rel):
        r.hincrby(_count_key(sid), "files", -1)
        r.hincrby(_count_key(sid), "bytes", -int(size))


def committed_files(sid: str) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for key, value in (_redis().hgetall(_files_key(sid)) or {}).items():
        rel = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        raw = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        size_text, _, sha = raw.partition(" ")
        try:
            out[rel] = (int(size_text), sha)
        except ValueError:
            continue
    return out


def session_status(sid: str, token: str) -> dict[str, Any]:
    state = _session(sid, token, allow_terminal=True)
    files = committed_files(sid)
    return {
        "ok": True,
        "status": state.get("status"),
        "run_name": state.get("run_name") or "",
        "server_case_dir": state.get("server_case_dir") or "",
        "keep_local": bool(state.get("keep_local")),
        "files": {rel: [size, sha] for rel, (size, sha) in files.items()},
    }


MAX_BUNDLE_FILES = 1000
MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def put_bundle(sid: str, token: str, body: bytes) -> dict[str, Any]:
    """Many small files in one request: a JSON line ({"files":[{rel,size,sha256,mtime_ms}],"total_files",
    "total_bytes"}), a newline, then the files' bytes back to back. Each file is written and verified on the
    server disk exactly like a chunked file; small files no longer cost two round trips each."""
    state = _session(sid, token)
    if len(body) > MAX_BUNDLE_BYTES + 4 * 1024 * 1024:
        raise StreamError(413, "bad_bundle", "Bundles are at most 64 MiB.")
    nl = body.find(b"\n")
    if nl <= 0:
        raise StreamError(400, "bad_bundle", "A bundle starts with one JSON line.")
    try:
        head = json.loads(body[:nl].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StreamError(400, "bad_bundle", "The bundle's first line is not JSON.") from exc
    items = head.get("files") if isinstance(head, dict) else None
    if not isinstance(items, list) or not items or len(items) > MAX_BUNDLE_FILES:
        raise StreamError(400, "bad_bundle", f"A bundle lists 1 to {MAX_BUNDLE_FILES} files.")
    data = memoryview(body)[nl + 1:]
    parsed: list[tuple[str, int, str, int]] = []
    total = 0
    for item in items:
        if not isinstance(item, dict):
            raise StreamError(400, "bad_bundle", "Bundle entries are objects.")
        rel = str(item.get("rel") or "")
        size = int(item.get("size") or 0)
        sha = str(item.get("sha256") or "").strip().lower()
        if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise StreamError(400, "bad_bundle", f"Bundle entry {rel[:200]} needs its size and SHA-256.")
        parsed.append((rel, size, sha, int(item.get("mtime_ms") or 0)))
        total += size
    if total != len(data):
        raise StreamError(400, "short_body", "The bundle's bytes do not match the listed sizes; send it again.")
    run = ""
    for rel, _size, _sha, _mt in parsed:
        parts = _rel_parts(state, rel)
        run = run or parts[1]
        if parts[1] != run:
            raise StreamError(409, "other_run", "A bundle holds files of one run.")
    _claim_run(state, run)
    results: list[dict[str, Any]] = []
    writer = _writer(state)
    pos = 0
    try:
        for rel, size, sha, mtime_ms in parsed:
            blob = bytes(data[pos:pos + size])
            pos += size
            if hashlib.sha256(blob).hexdigest() != sha:
                results.append({"rel": rel, "ok": False, "error": "hash_mismatch"})
                continue
            status, out = writer.chunk(rel, 0, blob)
            if status not in (200, 409) or (status == 409 and out.get("error") not in ("exists",)):
                results.append({"rel": rel, "ok": False, "error": str(out.get("error") or f"HTTP {status}")})
                continue
            status, out = writer.commit(ImportFile(share_rel="", rel=rel, size=size, mtime_ms=mtime_ms), sha)
            if status == 200 and out.get("ok") and str(out.get("sha256") or "").lower() == sha:
                _record_commit(sid, rel, size, sha, bool(out.get("exists")))
                results.append({"rel": rel, "ok": True, "exists": bool(out.get("exists"))})
            else:
                results.append({"rel": rel, "ok": False, "error": str(out.get("error") or f"HTTP {status}")})
    except HelperImportError as exc:
        raise StreamError(503, "server_helper_error", str(exc)) from exc
    finally:
        writer.close()
    _flush_progress(sid, (int(head.get("total_files") or 0), int(head.get("total_bytes") or 0)))
    return {"ok": all(r["ok"] for r in results), "results": results}


def discard_file(sid: str, token: str, rel: str) -> dict[str, Any]:
    """A file that changed on the client after it was sent: remove this session's server copy so the new
    version can be sent. Only files this session committed, only before the run is registered (sealed)."""
    state = _session(sid, token)
    _rel_parts(state, rel)
    have = committed_files(sid).get(rel)
    if not have:
        return {"ok": True, "removed": 0}
    writer = _writer(state)
    try:
        out = writer.discard([(have[1], rel)])
    except HelperImportError as exc:
        raise StreamError(503, "server_helper_error", str(exc)) from exc
    finally:
        writer.close()
    if not out.get("ok"):
        raise StreamError(409, "discard_failed", f"The server copy of {rel} could not be replaced: {out.get('error')}")
    _forget_commit(sid, rel, have[0])
    _flush_progress(sid)
    return {"ok": True, "removed": int(out.get("removed") or 0)}


def finish(
    sid: str,
    token: str,
    *,
    run_name: str,
    files: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    client_host: str = "",
    client_case_dir: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    state = _session(sid, token, allow_terminal=True)
    if state.get("status") == "completed":
        return {"ok": True, "already": True, "server_case_dir": state.get("server_case_dir") or "",
                "hash_list": (state.get("result") or {}).get("client_import", {}).get("hash_list", "")}
    if state.get("status") in TERMINAL:
        raise StreamError(409, str(state.get("status")), f"This upload session is already {state.get('status')}.")
    run = (run_name or "").strip()
    if not run or not safe_segment(run):
        raise StreamError(400, "bad_run", "finish needs the run name")
    if state.get("run_name") and state["run_name"] != run:
        raise StreamError(409, "other_run", f"This session receives run {state['run_name']}, not {run}.")
    state["run_name"] = run
    if client_host:
        state["client_host"] = client_host[:120]
    if client_case_dir:
        state["client_case_dir"] = client_case_dir[:1024]
    committed = committed_files(sid)
    missing: list[str] = []
    listed: dict[str, tuple[int, str]] = {}
    for item in files or []:
        rel = str(item.get("rel") or "")
        parts = _rel_parts(state, rel)
        if parts[1] != run:
            raise StreamError(409, "other_run", f"{rel} belongs to another run.")
        size = int(item.get("size") or 0)
        sha = str(item.get("sha256") or "").lower()
        listed[rel] = (size, sha)
        have = committed.get(rel)
        if not have or have[0] != size:
            missing.append(rel)
        elif sha and have[1] and have[1] != sha:
            raise StreamError(
                409, "changed",
                f"{rel} changed on the client after it was sent; the server keeps the first verified copy.",
            )
    if missing:
        return {"ok": False, "missing": missing[:500], "missing_count": len(missing)}
    if not listed:
        raise StreamError(400, "empty", "The collection has no files to register.")

    run_warnings = list(state.get("warnings") or []) + [str(w)[:500] for w in (warnings or [])][:50]
    extra = sorted(rel for rel in committed if rel not in listed)
    if extra:
        # Sent while the collection ran, then removed by the collector on the client. The server copy
        # is verified and is never deleted, so it is registered too.
        run_warnings.append(
            f"{len(extra)} file(s) were sent while the collection ran and later removed on the client "
            "computer; the forensic server keeps the verified copies (listed in the hash list)."
        )
        for rel in extra:
            listed[rel] = committed[rel]
    state.update({"stage": "sealing", "detail": "Marking the server copy of the original extraction read-only"})
    save_state(state)
    writer = _writer(state)
    try:
        sealed = writer.seal(sorted(rel for rel in listed if rel.startswith("02_Original_Extraction/")))
        if not sealed.get("ok") or int(sealed.get("failed") or 0):
            run_warnings.append(
                "Some files of the server copy of 02_Original_Extraction could not be marked read-only "
                f"({sealed.get('failed') or sealed.get('error')})."
            )
        case_dir = str(state.get("server_case_dir") or "")
        hash_rel = f"08_Hashes/{run}/client_stream_{sid}_sha256.txt"
        hash_lines = "".join(f"{committed[rel][1]}  {rel}\n" for rel in sorted(listed))
        hash_sha = _put_small_file(writer, hash_rel, hash_lines.encode("utf-8"))
        total_bytes = sum(size for size, _sha in listed.values())
        state.update({
            "total_files": len(listed),
            "total_bytes": total_bytes,
            "files_done": len(listed),
            "bytes_done": total_bytes,
            "ended_utc": _now(),
        })
        provenance = {
            "schema": SCHEMA,
            "session_id": sid,
            "run_name": run,
            "scope": state["scope"],
            "examiner": state.get("examiner") or "",
            "started_utc": state["started_utc"],
            "completed_utc": state["ended_utc"],
            "client": {
                "hostname": state.get("client_host") or "",
                "case_dir": state.get("client_case_dir") or "",
                "keeps_local_copy": bool(state.get("keep_local")),
                "transport": "client_stream",
            },
            "server": {
                "case_dir": case_dir,
                "case_folder": state["server_case_folder"],
                "helper": state.get("server_helper") or {},
                "paths": server_paths(case_dir, run, state["scope"]),
            },
            "files": {"count": len(listed), "bytes": total_bytes},
            "verification": (
                "Each file was hashed (SHA-256) on the client while it was sent and again on the forensic server disk "
                "before it received its final name; both digests matched. Files are never overwritten on the server."
            ),
            "hash_list": hash_rel,
            "hash_list_sha256": hash_sha,
            "sealed": sealed,
            "warnings": run_warnings,
        }
        _put_small_file(writer, f"07_Logs/{run}/client_stream_{sid}.json", json.dumps(provenance, indent=2).encode("utf-8"))
    except HelperImportError as exc:
        raise StreamError(503, "server_helper_error", f"Registering the server copy failed: {exc}") from exc
    finally:
        writer.close()

    family = ""
    if isinstance(summary, dict):
        device = summary.get("device") or summary.get("device_profile") or {}
        if isinstance(device, dict):
            family = str(device.get("os_family") or "").lower()
    selection = RunSelection(
        case_prefix="",
        case_folder=state["client_case_folder"],
        run_name=run,
        files=[],
        has_summary=f"07_Logs/{run}/collection_summary.json" in listed,
    )
    result = _build_result(state, selection, summary if isinstance(summary, dict) else None, case_dir, run_warnings)
    result["client_import"]["hash_list"] = _win_join(case_dir, *hash_rel.split("/"))
    result["client_import"]["provenance"] = _win_join(case_dir, "07_Logs", run, f"client_stream_{sid}.json")
    state.update({
        "status": "completed",
        "stage": "complete",
        "detail": f"Collected on {state.get('client_host') or 'the client computer'} and stored on the forensic server in {case_dir}",
        "warnings": run_warnings,
        "result": result,
        "run": {
            "run_id": "",
            "run_name": run,
            "case_id": state.get("case_id") or state["client_case_folder"],
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
                "detail": "Collected on the client computer and sent to the forensic server",
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
    log.info("client stream %s complete: %s files, %s bytes -> %s", sid, state["total_files"], state["total_bytes"], case_dir)
    return {"ok": True, "server_case_dir": case_dir, "hash_list": _win_join(case_dir, *hash_rel.split("/")),
            "hash_list_sha256": hash_sha}


def abort(sid: str, token: str, reason: str = "") -> dict[str, Any]:
    state = _session(sid, token, allow_terminal=True)
    if state.get("status") not in TERMINAL:
        state.update({
            "status": "failed",
            "stage": "failed",
            "detail": "The collection on the client computer did not finish",
            "error": (reason or "The client stopped sending this collection.")[:1000],
            "ended_utc": _now(),
        })
        save_state(state)
    return {"ok": True, "status": state.get("status")}


__all__ = [
    "MAX_CHUNK_BYTES",
    "StreamError",
    "TOKEN_HEADER",
    "abort",
    "client_case_folder",
    "commit_file",
    "committed_files",
    "create_session",
    "decode_rel",
    "discard_file",
    "finish",
    "put_bundle",
    "put_chunk",
    "session_status",
    "stream_chunk_bytes",
]
