"""Verified direct extracted-shard reads from an already claimed inventory row.

Only authoritative extracted-part metadata may use this optimization. Missing or
special derived sources must use the existing database-backed reader.
"""
from __future__ import annotations

import hashlib
import json


def read_claimed_member(row, path, max_bytes, read_member):
    """Return (handled, bytes). handled=False means use the original read path.

    Never silently truncate forensic evidence or accept unverified content.
    The exact source is taken from the case's claimed inventory payload.
    """
    if not isinstance(row, dict) or str(row.get("path") or "").replace("\\", "/") != path.replace("\\", "/"):
        return False, None
    meta = row.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            return False, None
    if not isinstance(meta, dict) or meta.get("whatsapp_derivation"):
        return False, None
    part = meta.get("extracted_part_uri")
    if not isinstance(part, str) or not part:
        return False, None
    size = row.get("size")
    digest = row.get("sha256") or meta.get("source_sha256")
    # Requires complete, independently verifiable manifest metadata.
    if size is None or not digest:
        return False, None
    size = int(size)
    if size < 0:
        return False, None
    if max_bytes is not None and size > int(max_bytes):
        # Preserve the existing path's explicit read ceiling handling.
        return False, None
    data = read_member(part, path, max_bytes=size + 1)
    if data is None:
        return True, None
    if len(data) != size:
        raise ValueError(f"Extracted evidence size mismatch: {path}")
    if hashlib.sha256(data).hexdigest().lower() != str(digest).lower():
        raise ValueError(f"Extracted evidence SHA-256 mismatch: {path}")
    return True, data
