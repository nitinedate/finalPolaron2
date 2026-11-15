"""Register already-decrypted WhatsApp SQLite files from the current case working copy.

Runs only in Android mobile reprocessing, never reads a device or sealed original,
never moves plaintext into an original ZIP, and never stores WhatsApp keys.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger("mobile_forensic.whatsapp_working_copy_intake")
_MAX_BYTES = 768 * 1024 * 1024
_CASE_RE = re.compile(r"(CASE-[A-Za-z0-9-]+)", re.IGNORECASE)


def _evidence_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for item in value.values():
            found.extend(_evidence_strings(item))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for item in value:
            found.extend(_evidence_strings(item))
        return found
    return []


def case_id_from_evidence(disk_source: dict[str, Any], artifact_paths: list[str]) -> str | None:
    """Use a case identifier explicitly associated with this job, never a global scan."""
    for value in [*_evidence_strings(disk_source), *artifact_paths]:
        match = _CASE_RE.search(value)
        if match:
            return match.group(1)
    return None


def host_case_roots(disk_source: dict[str, Any] | None) -> list[Path]:
    """Folders that contain CASE-* directories.

    The Android worker mounts the project evidence tree at /host/<drive>/...,
    while /evidence is a different store. A Windows case path on D: has to be
    read from that host mount or the plaintext msgstore.db is never parsed.
    """
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path).replace("\\", "/").rstrip("/").lower()
        if not key or key in seen:
            return
        seen.add(key)
        found.append(path)

    add(Path(os.environ.get("POLARON_EVIDENCE_CASES_ROOT", "/evidence/cases")))
    marker = "/evidence/cases"
    for raw_value in _evidence_strings(disk_source):
        raw = raw_value.replace("\\", "/")
        idx = raw.lower().find(marker)
        if idx < 0:
            continue
        root_text = raw[: idx + len(marker)]
        if not (root_text.startswith("/") or (len(root_text) >= 3 and root_text[1] == ":")):
            continue
        add(Path(root_text))
        if len(root_text) >= 3 and root_text[1] == ":":
            letter = root_text[0].lower()
            rest = root_text[3:].lstrip("/")
            add(Path("/host") / letter / rest)
    return found


def working_copy_candidates(case_dir: Path) -> list[Path]:
    """Locate only expected derived database paths in the case working-copy tree."""
    base = case_dir / "03_Working_Copy"
    if not base.is_dir() or base.is_symlink():
        return []
    result = []
    for run_dir in base.iterdir():
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        for rel in ("readable_artifacts/whatsapp/Databases/msgstore.db",
                    "readable_artifacts/whatsapp/databases/msgstore.db",
                    "whatsapp/Databases/msgstore.db"):
            path = run_dir / rel
            if (path.is_file() and not path.is_symlink()
                    and path.resolve().is_relative_to(base.resolve())):
                result.append(path)
    return result


def validate_whatsapp_sqlite(path: Path) -> tuple[int, list[str]]:
    """Validate database structure without writing to the evidence source."""
    size = path.stat().st_size
    if not 100 <= size <= _MAX_BYTES:
        raise ValueError("plaintext file size is invalid or above safety limit")
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise ValueError("not SQLite plaintext")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        checked = con.execute("PRAGMA quick_check(1)").fetchone()
        if checked is None or checked[0] != "ok":
            raise ValueError("SQLite quick_check failed")
        tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if not any(t.lower() in {"message", "messages", "message_v2", "chat", "chat_list", "chat_view"} for t in tables):
            raise ValueError("SQLite has no recognizable WhatsApp message/chat tables")
    finally:
        con.close()
    return size, tables


def register_working_copy_whatsapp(db, job_id: str, disk_source: dict[str, Any],
                                   artifact_paths: list[str]) -> dict[str, Any]:
    from app.db.sql_helpers import execute, fetchone
    from app.services.storage import put_bytes

    result: dict[str, Any] = {"state": "not_found", "registered": 0, "cached": 0, "rejected": []}
    case_id = case_id_from_evidence(disk_source, artifact_paths)
    if not case_id:
        result["state"] = "case_not_identified"
        return result
    # Backend may run inside Docker. /evidence is not the examiner case tree;
    # the same CASE folder is also on the host drive mount.
    candidates: list[Path] = []
    for root in host_case_roots(disk_source):
        if not root.is_dir() or root.is_symlink():
            continue
        candidates.extend(working_copy_candidates(root / case_id))
    if not candidates:
        result["state"] = "not_found_or_case_volume_not_mounted"
        return result
    for path in candidates:
        try:
            size, tables = validate_whatsapp_sqlite(path)
            # Copy the validated source into case-owned object storage. Do not
            # make readers depend on mutable host file paths after this step.
            data = path.read_bytes()
            if len(data) != size:
                raise ValueError("source changed during reading")
            digest = hashlib.sha256(data).hexdigest()
            logical = f"derived/mobile/android/whatsapp_decrypted/working_copy/{digest[:24]}/msgstore.db"
            prior = fetchone(db, "SELECT sha256 FROM job_artifacts WHERE job_id=:jid AND file_path=:path",
                             {"jid": job_id, "path": logical})
            if prior and prior.get("sha256") == digest:
                result["cached"] += 1
                continue
            uri = put_bytes(f"jobs/{job_id}/{logical}", data, "application/vnd.sqlite3")
            metadata = {
                "whatsapp_working_copy_intake": True,
                "source_case_id": case_id,
                "source_relpath": str(path.relative_to(path.parents[4])).replace("\\", "/"),
                "decryption_authenticated": None,  # external process; cannot attest to crypt15 GCM here
                "database_validated": True,
                "sqlite_tables": tables[:50],
                "source_sha256": digest,
                "mobile_platform": "android",
            }
            execute(db, """INSERT INTO job_artifacts
                (job_id,file_path,file_name,extension,size_bytes,sha256,minio_uri,parse_status,metadata)
                VALUES (:jid,:path,'msgstore.db','.db',:size,:sha,:uri,'pending',CAST(:meta AS jsonb))
                ON CONFLICT (job_id,file_path) DO UPDATE SET
                minio_uri=EXCLUDED.minio_uri, size_bytes=EXCLUDED.size_bytes,
                sha256=EXCLUDED.sha256,parse_status='pending',
                metadata=coalesce(job_artifacts.metadata,'{}'::jsonb)||EXCLUDED.metadata,
                updated_at=NOW()""",
                {"jid": job_id, "path": logical, "size": size, "sha": digest,
                 "uri": uri, "meta": json.dumps(metadata)},
            )
            result["registered"] += 1
        except Exception as exc:
            log.warning("working-copy WhatsApp import rejected job=%s file=%s reason=%s", job_id, path, exc)
            result["rejected"].append({"path": str(path), "reason": str(exc)[:200]})
    result["state"] = "ready" if result["registered"] or result["cached"] else "invalid"
    return result
