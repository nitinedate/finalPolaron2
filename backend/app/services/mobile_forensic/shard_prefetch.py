"""Bounded single-pass prefetch for small mobile evidence files.

A claim remains a collection of independently committed evidence bundles. This
module only changes how verified source bytes are retrieved, never which items
are marked done or how database/sidecar groups are parsed.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict

log = logging.getLogger(__name__)

# Bound memory per worker (8 workers => at most 8 x configured limit).  Large
# SQLite databases, media and unsupported metadata continue down the existing
# source-reader path. Do not create persistent evidence copies on local disk.
MAX_MEMBER = max(1024, min(int(os.getenv("MOBILE_PREFETCH_MAX_MEMBER_BYTES", "8388608")), 32 * 1024 * 1024))
MAX_BATCH = max(MAX_MEMBER, min(int(os.getenv("MOBILE_PREFETCH_MAX_BATCH_BYTES", "67108864")), 128 * 1024 * 1024))
# Avoid lengthy full-archive scans for a batch scattered over many parts.
MAX_SHARDS_PER_CLAIM = max(1, min(int(os.getenv("MOBILE_PREFETCH_MAX_SHARDS", "2")), 8))
MIN_MEMBERS_PER_SHARD = max(2, min(int(os.getenv("MOBILE_PREFETCH_MIN_GROUP", "2")), 24))


def _members(claims):
    for claimed in claims:
        payload = claimed.get("payload") or []
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, list):
            continue
        for row in payload:
            if isinstance(row, dict):
                yield row


def choose_prefetch_groups(claims, *, wanted=None, max_member=MAX_MEMBER, max_batch=MAX_BATCH, retain_bytes=None):
    """Select only small members with known part URIs and bounded total bytes.

    Grouping depends only on immutable source metadata, not on the worker's DB
    state. A part with one selected member is left to the normal read path.
    ``retain_bytes`` is the prefix a parser will keep. The batch budget uses
    that prefix, while the returned size stays the real source size so hash
    and length checks still cover the whole member. Without this, a 5 MB
    cache file that only needs a 4 KB header consumes a full slot and every
    later file scans the archive from the start.
    """
    # First group *all* eligible metadata by source shard.  The old code
    # deducted memory before ranking shards, so 256 random small files could
    # exhaust MAX_BATCH and prevent the densely populated shard from being
    # prefetched at all.  This phase stores metadata only, not file contents.
    grouped = defaultdict(list)
    seen = set()
    retain = retain_bytes or {}
    for row in _members(claims):
        path = str(row.get("path") or "").replace("\\", "/")
        if not path or (wanted is not None and path not in wanted) or path in seen:
            continue
        seen.add(path)
        meta = row.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}
        if not isinstance(meta, dict) or meta.get("whatsapp_derivation"):
            continue
        part = meta.get("extracted_part_uri")
        try:
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        retained = size
        if path in retain:
            try:
                retained = min(size, max(1, int(retain[path])))
            except (TypeError, ValueError):
                retained = size
        if not part or size <= 0 or retained <= 0 or retained > max_member:
            continue
        grouped[str(part)].append((path, size, row.get("sha256") or meta.get("source_sha256"), retained))

    # Prioritize reuse (members per scan), and only then allocate the bounded
    # cache budget.  A whole shard is still streamed only once in this batch.
    candidates = [(part, entries) for part, entries in grouped.items()
                  if len(entries) >= MIN_MEMBERS_PER_SHARD]
    candidates.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    selected = {}
    remaining = max(0, int(max_batch))
    for part, entries in candidates[:MAX_SHARDS_PER_CLAIM]:
        keep = []
        for path, size, digest, retained in entries:
            if retained <= remaining:
                keep.append((path, size, digest))
                remaining -= retained
        if len(keep) >= MIN_MEMBERS_PER_SHARD:
            selected[part] = keep
    return selected


def prefetch_claim_bytes(claims, registry, context, *, on_detail=None):
    """Fetch hash/size-verified source bytes once per eligible shard per claim.

    This is a performance hint, not an authoritative evidence source. A failed
    prefetch is retried by the existing per-file reader (which reports read
    failures and verifies original source hashes). Never hide errors.
    """
    if os.getenv("MOBILE_SHARD_PREFETCH", "true").strip().lower() in {"0", "false", "no"}:
        return 0
    from app.services.mobile_forensic.models import InventoryItem
    from app.services.tar_cache import iter_files_from_part
    wanted = set()
    read_caps = {}
    for row in _members(claims):
        try:
            item = InventoryItem(**row)
            parsers = registry.route(item, context)
            names = {getattr(parser, "name", "") for parser in parsers}
            path = item.path.replace("\\", "/")
            if not parsers or names == {"files_media_parser"}:
                continue  # File/media metadata is already in the manifest.
            wanted.add(path)
            # A fallback-only parser needs just an evidence header; avoid
            # retaining a complete multi-megabyte body unnecessarily.
            if names == {"forensic_fallback"} and (item.extension or "").lower() not in {".eml", ".emlx", ".mbox", ".msg"}:
                read_caps[path] = min(int(item.size or 0), 4096)
            else:
                read_caps[path] = int(item.size or 0)
        except Exception:
            # An unusual router must be handled by the normal parser, which
            # records the original exception and retains its source metadata.
            continue
    # Companion sidecars must stay together with their SQLite database.
    # Include a sidecar only when its base source has a native parser.
    for row in _members(claims):
        path = str(row.get("path") or "").replace("\\", "/")
        if any(path.lower().endswith(s) and path[:-len(s)] in wanted
               for s in ("-wal", "-shm", "-journal")):
            wanted.add(path)
    groups = choose_prefetch_groups(claims, wanted=wanted, retain_bytes=read_caps)
    cache = context.extra["prefetched_bytes"]
    total = 0
    for part, entries in groups.items():
        sizes = {p: size for p, size, _ in entries}
        caps = {p: read_caps.get(p, size) for p, size, _ in entries}
        hashes = {p: digest for p, _, digest in entries if digest}
        try:
            for path, data in iter_files_from_part(
                part,
                set(caps),
                max_bytes_by_path=caps,
                verify_sha256_by_path=hashes,
                verify_size_by_path=sizes,
                on_detail=on_detail,
            ):
                # A None value indicates missing/corrupt evidence; its normal
                # read path will still classify and record the error.
                if data is None or len(data) != caps[path]:
                    continue
                cache[path] = (data, sizes[path])
                total += len(data)
        except Exception:
            log.warning("Mobile prefetch failed; individual verified reads will retry", exc_info=True)
    return total
