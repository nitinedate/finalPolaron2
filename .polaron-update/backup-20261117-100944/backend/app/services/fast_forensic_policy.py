"""Conservative metadata-only classification for Fast Forensic mode.

This is a scheduling choice, NOT an evidence-recovery or evidentiary-coverage claim.
Named databases, plists and their sidecars stay eligible for native parsers.
Inventory/preview/OCR/media stages operate independently.

iOS logical images also store every backup file a second time as a 40-hex
blob under ios_image/<udid>/<aa>/<hash>. Those copies have no domain filename,
so native parsers cannot route them; the named file under readable_artifacts
is the one that is parsed. Thumbnails are the same kind of bulk payload.
"""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath

# Bulk media, thumbnail and non-forensic executable payloads are still
# registered with their hashes/paths and available to other evidence stages.
_METADATA_ONLY_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".bmp", ".tif", ".tiff", ".avif", ".dng",
    ".mp4", ".mov", ".m4v", ".3gp", ".3gpp", ".avi", ".mkv", ".webm",
    ".mp3", ".aac", ".m4a", ".wav", ".flac", ".opus", ".amr", ".ogg",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".exe", ".dll", ".so", ".dylib", ".ttf", ".otf", ".woff", ".woff2",
    ".thumb", ".thumbnail",
})
# ios_image/<device>/<2 hex>/<40 hex> — the unnamed backup copy.
_IOS_BACKUP_BLOB = re.compile(r"/ios_image/[^/]+/[0-9a-f]{2}/[0-9a-f]{40}$", re.IGNORECASE)
# Do not classify unknown or forensic-rich records as metadata-only.
_NATIVE_MARKERS = (
    "sqlite", "database", "msgstore", "chatstorage", "mmssms", "sms.db",
    "contacts", "callhistory", "calllog", "history.db", "history.sqlite",
    "cookies", "keychain", "backup", "manifest", "wal", "journal",
    "deleted", "recovered", "carved", "location", "knowledgec", "places.sqlite",
)


def fast_forensic_enabled() -> bool:
    return os.getenv("FORENSIC_PROCESSING_MODE", "fast_forensic").strip().lower() in {
        "fast", "fast_forensic", "targeted"
    }


def metadata_only_path(path: str, extension: str | None = None) -> bool:
    """True when this source stays indexed and does not need a native parser."""
    normalized = str(path or "").replace("\\", "/").lower()
    if not normalized:
        return False
    ext = (extension or PurePosixPath(normalized).suffix or "").lower()
    if any(part in normalized for part in _NATIVE_MARKERS):
        return False
    if normalized.endswith(("-wal", "-shm", "-journal")):
        return False
    if _IOS_BACKUP_BLOB.search(normalized):
        return True
    return ext in _METADATA_ONLY_EXTENSIONS


def metadata_only_bundle(bundle) -> bool:
    """Only skip native parsing when every member is conservatively classified.

    Database companions automatically force the whole group through parsing.
    """
    if not bundle:
        return False
    for item in bundle:
        path = str(getattr(item, "path", "") or "")
        ext = getattr(item, "extension", None)
        if not metadata_only_path(path, ext):
            return False
    return True
