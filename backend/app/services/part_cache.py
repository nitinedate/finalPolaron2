"""Seekable local cache of extracted ``.tar.zst`` parts (V1.28.0 performance).

Why
---
Extraction stores each shard as ONE zstd frame around a tar stream (2–20 GB per
part). zstd frames are not seekable, so reading one member meant streaming the
part from MinIO and decompressing it from byte 0 until that member appeared.
Native parsing, recovery, OCR, RAG chunking and artifact preview read members
in many separate calls, so the same multi-GB shard was decompressed again and
again — thousands of times on a 100k+ file phone case.

What
----
The first time a process needs to scan the same part a second time (or at once
with ``EXTRACT_PART_CACHE=always``), the part is decompressed ONCE into a plain
tar on the scratch volume and its member data offsets are indexed. Later reads
are a seek + read of exactly the bytes of that member.

Forensic rules
--------------
* The stored evidence (MinIO parts, manifests, hashes) is never modified. The
  cache is a disposable working copy that can be deleted at any time.
* The cached tar is byte-for-byte the decompressed part, so every member read
  returns the identical bytes the streaming reader returns. Callers' own size /
  SHA-256 checks still run on the bytes read (``tar_cache`` does that).
* A cache entry is used only while the object's size + ETag/mtime still match
  the object it was built from; a re-extracted part is rebuilt, never mixed.
* A build that cannot complete (decompression/tar error, disk getting full,
  stop request) is discarded and the caller falls back to the original
  streaming read, so behaviour never gets worse than before.
* Bounded: a byte budget and a minimum free-space floor; least-recently-used
  entries are evicted.

Settings (all optional)
-----------------------
``EXTRACT_PART_CACHE``          auto (default) | always | off
``EXTRACT_PART_CACHE_DIR``      default /scratch/part-cache, else $DATA_ROOT/part-cache
``EXTRACT_PART_CACHE_GB``       hard budget; default auto = min(25% of the disk, 50% of the space above the floor)
``EXTRACT_PART_CACHE_MIN_FREE_GB``  keep at least this much free (default 40)
``EXTRACT_PART_CACHE_SCANS``    auto mode builds on this scan of a part (default 2)
``EXTRACT_PART_CACHE_MAX_IDLE_HOURS``  entries unused this long are removed (default 12)

The cache always gives way to evidence: extraction calls :func:`release_space`
before writing a shard, and any entry can be deleted at any time.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import tarfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("part_cache")

VERSION = 1
_GIB = 1024 ** 3
_CHUNK = 8 * 1024 * 1024
_FREE_CHECK_EVERY = 512 * 1024 * 1024

_lock = threading.Lock()
_scan_counts: dict[str, tuple[int, float]] = {}
_index_cache: "OrderedDict[str, CachedPart]" = OrderedDict()
_INDEX_CACHE_MAX = 24
_build_locks: dict[str, threading.Lock] = {}
_failed_builds: dict[str, float] = {}
_FAILED_RETRY_SEC = 600.0
_stats = {"hits": 0, "builds": 0, "build_failures": 0, "fallbacks": 0, "evictions": 0, "bytes_served": 0}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def mode() -> str:
    value = (os.environ.get("EXTRACT_PART_CACHE") or "auto").strip().lower()
    if value in {"0", "false", "no", "off", "disabled"}:
        return "off"
    if value in {"always", "eager", "1", "true", "yes", "on"}:
        return "always"
    return "auto"


def enabled() -> bool:
    return mode() != "off"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def build_after_scans() -> int:
    return max(1, int(_env_float("EXTRACT_PART_CACHE_SCANS", 2)))


def min_free_bytes() -> int:
    return int(max(1.0, _env_float("EXTRACT_PART_CACHE_MIN_FREE_GB", 40.0)) * _GIB)


def max_idle_seconds() -> float:
    return max(0.1, _env_float("EXTRACT_PART_CACHE_MAX_IDLE_HOURS", 12.0)) * 3600.0


def cache_root() -> Path:
    configured = (os.environ.get("EXTRACT_PART_CACHE_DIR") or "").strip()
    if configured:
        return Path(configured)
    scratch = Path((os.environ.get("EXTRACT_SCRATCH_DIR") or "/scratch").strip() or "/scratch")
    if scratch.is_dir() and os.access(scratch, os.W_OK):
        return scratch / "part-cache"
    data_root = (os.environ.get("DATA_ROOT") or "/app/data").strip() or "/app/data"
    return Path(data_root) / "part-cache"


def _key(part_uri: str) -> str:
    return hashlib.sha256(part_uri.encode("utf-8")).hexdigest()[:40]


def stats() -> dict[str, int]:
    with _lock:
        return dict(_stats)


def _count(name: str, n: int = 1) -> None:
    with _lock:
        _stats[name] = _stats.get(name, 0) + n


# ---------------------------------------------------------------------------
# Object identity (the cache must belong to *this* version of the part)
# ---------------------------------------------------------------------------

_identity_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _identity_ttl() -> float:
    # Default 0: ask the object store on every read (~2 ms), so a re-extracted part
    # can never be answered from the copy of its previous version.
    return max(0.0, min(_env_float("EXTRACT_PART_CACHE_IDENTITY_TTL_SEC", 0.0), 60.0))


def object_identity(part_uri: str, *, fresh: bool = False) -> dict[str, Any] | None:
    """Size + version of the stored part (optionally remembered for a few seconds).

    Builds always ask again (``fresh=True``).
    """
    now = time.monotonic()
    ttl = _identity_ttl()
    if not fresh and ttl > 0:
        with _lock:
            hit = _identity_cache.get(part_uri)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    try:
        from app.services.storage import object_identity as _identity

        value = _identity(part_uri)
    except Exception:
        log.debug("object identity unavailable for %s", part_uri, exc_info=True)
        value = None
    with _lock:
        _identity_cache[part_uri] = (now, value)
        if len(_identity_cache) > 4096:
            for stale in [k for k, (at, _) in _identity_cache.items() if now - at > 60.0]:
                _identity_cache.pop(stale, None)
    return value


def _same_identity(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not a or not b:
        return False
    return int(a.get("size") or -1) == int(b.get("size") or -2) and str(a.get("version") or "") == str(b.get("version") or "")


# ---------------------------------------------------------------------------
# Cached part
# ---------------------------------------------------------------------------

@dataclass
class CachedPart:
    part_uri: str
    entry_dir: Path
    tar_path: Path
    identity: dict[str, Any]
    # member name -> [(data_offset, size), ...] in archive order
    members: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    member_count: int = 0
    tar_bytes: int = 0
    _touched: float = 0.0

    def occurrences(self, name: str) -> list[tuple[int, int]]:
        return self.members.get(name) or []

    def position(self, offset: int) -> int:
        """Approximate archive position (members before ``offset``) for progress reporting."""
        return 0 if not self.tar_bytes else int(self.member_count * min(1.0, offset / self.tar_bytes))

    def touch(self) -> None:
        now = time.time()
        if now - self._touched < 60:
            return
        self._touched = now
        try:
            os.utime(self.entry_dir / "meta.json", None)
        except OSError:
            pass

    def open(self):
        return open(self.tar_path, "rb", buffering=0)


def _load_entry(part_uri: str, entry_dir: Path) -> CachedPart | None:
    meta_path = entry_dir / "meta.json"
    index_path = entry_dir / "index.json"
    tar_path = entry_dir / "part.tar"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if int(meta.get("version") or 0) != VERSION or meta.get("part_uri") != part_uri:
            return None
        if tar_path.stat().st_size != int(meta.get("tar_bytes") or -1):
            return None
        rows = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    members: dict[str, list[tuple[int, int]]] = {}
    for name, offset, size in rows:
        members.setdefault(name, []).append((int(offset), int(size)))
    return CachedPart(
        part_uri=part_uri,
        entry_dir=entry_dir,
        tar_path=tar_path,
        identity=dict(meta.get("identity") or {}),
        members=members,
        member_count=len(rows),
        tar_bytes=int(meta.get("tar_bytes") or 0),
    )


def _remember(part: CachedPart) -> None:
    with _lock:
        _index_cache[part.part_uri] = part
        _index_cache.move_to_end(part.part_uri)
        while len(_index_cache) > _INDEX_CACHE_MAX:
            _index_cache.popitem(last=False)


def _forget(part_uri: str) -> None:
    with _lock:
        _index_cache.pop(part_uri, None)


def lookup(part_uri: str, identity: dict[str, Any] | None = None) -> CachedPart | None:
    """Return the cached part if present and built from the current object."""
    if not enabled() or not part_uri:
        return None
    identity = identity if identity is not None else object_identity(part_uri)
    if not identity:
        return None
    with _lock:
        part = _index_cache.get(part_uri)
    if part is not None and _same_identity(part.identity, identity) and part.tar_path.exists():
        part.touch()
        return part
    entry_dir = cache_root() / _key(part_uri)
    if not entry_dir.is_dir():
        return None
    part = _load_entry(part_uri, entry_dir)
    if part is None or not _same_identity(part.identity, identity):
        if part is not None:
            # The part was re-extracted: the old copy must never be served.
            _remove_entry(entry_dir)
        _forget(part_uri)
        return None
    _remember(part)
    part.touch()
    return part


def note_scan(part_uri: str) -> int:
    """Count full-part scans in this process (auto mode builds on the Nth)."""
    now = time.monotonic()
    with _lock:
        count, first = _scan_counts.get(part_uri, (0, now))
        if now - first > 3600:
            count, first = 0, now
        count += 1
        _scan_counts[part_uri] = (count, first)
        if len(_scan_counts) > 4096:
            for stale in [k for k, (_, t) in _scan_counts.items() if now - t > 3600]:
                _scan_counts.pop(stale, None)
        return count


def acquire(part_uri: str) -> CachedPart | None:
    """Cached part for a read, building it when the policy says so; None → stream as before."""
    if not enabled() or not part_uri:
        return None
    identity = object_identity(part_uri)
    if not identity:
        return None
    _housekeeping()
    part = lookup(part_uri, identity)
    if part is not None:
        _count("hits")
        return part
    scans = note_scan(part_uri)
    if mode() == "auto" and scans < build_after_scans():
        return None
    with _lock:
        failed_at = _failed_builds.get(part_uri)
    if failed_at and time.monotonic() - failed_at < _FAILED_RETRY_SEC:
        return None
    part = build(part_uri, identity)
    if part is not None:
        _count("hits")
    return part


# ---------------------------------------------------------------------------
# Disk budget
# ---------------------------------------------------------------------------

def _disk_free(path: Path) -> int:
    try:
        usage = shutil.disk_usage(path)
        return int(usage.free)
    except OSError:
        return 0


def _disk_total(path: Path) -> int:
    try:
        return int(shutil.disk_usage(path).total)
    except OSError:
        return 0


def _entry_bytes(entry_dir: Path) -> int:
    total = 0
    try:
        for child in entry_dir.iterdir():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _entries(root: Path) -> list[tuple[float, Path, int]]:
    out = []
    try:
        children = list(root.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        meta = child / "meta.json"
        try:
            used = meta.stat().st_mtime
        except OSError:
            used = 0.0
        out.append((used, child, _entry_bytes(child)))
    return sorted(out, key=lambda row: row[0])


def budget_bytes(root: Path | None = None) -> int:
    root = root or cache_root()
    configured = _env_float("EXTRACT_PART_CACHE_GB", 0.0)
    if configured > 0:
        return int(configured * _GIB)
    used = sum(size for _, _, size in _entries(root))
    above_floor = _disk_free(root) + used - min_free_bytes()
    return max(0, int(min(0.25 * _disk_total(root), 0.5 * above_floor)))


def _remove_entry(entry_dir: Path) -> bool:
    lock_path = entry_dir.parent / f".{entry_dir.name}.lock"
    try:
        with open(lock_path, "a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False  # being built or rebuilt by someone else
            shutil.rmtree(entry_dir, ignore_errors=True)
            return not entry_dir.exists()
    except OSError:
        shutil.rmtree(entry_dir, ignore_errors=True)
        return not entry_dir.exists()


def _evict(entry_dir: Path, size: int, why: str) -> bool:
    if not _remove_entry(entry_dir):
        return False
    _count("evictions")
    with _lock:
        for uri, part in list(_index_cache.items()):
            if part.entry_dir == entry_dir:
                _index_cache.pop(uri, None)
    log.info("part-cache removed %s (%.1f GB, %s)", entry_dir.name, size / _GIB, why)
    return True


def remove_idle() -> int:
    """Drop entries nobody has read for EXTRACT_PART_CACHE_MAX_IDLE_HOURS (finished jobs)."""
    cutoff = time.time() - max_idle_seconds()
    removed = 0
    for used, entry_dir, size in _entries(cache_root()):
        if used < cutoff and _evict(entry_dir, size, "idle"):
            removed += 1
    return removed


def release_space(needed: int = 0) -> int:
    """Give disk space back to evidence: evict LRU entries until ``needed`` bytes fit above the floor.

    Extraction calls this before spooling/uploading a shard so the cache never
    competes with evidence storage on the same drive. Returns bytes released.
    """
    root = cache_root()
    if not root.is_dir():
        return 0
    released = 0
    for _, entry_dir, size in _entries(root):
        if _disk_free(root) - max(0, int(needed)) >= min_free_bytes():
            break
        if _evict(entry_dir, size, "space needed for evidence"):
            released += size
    return released


_last_housekeeping = [0.0]


def _housekeeping() -> None:
    now = time.monotonic()
    if now - _last_housekeeping[0] < 600:
        return
    _last_housekeeping[0] = now
    try:
        remove_idle()
        release_space(0)
    except Exception:
        log.debug("part-cache housekeeping failed", exc_info=True)


def make_room(needed: int, *, keep: str = "") -> bool:
    """Evict least-recently-used entries until ``needed`` bytes fit the budget and free floor."""
    root = cache_root()
    remove_idle()
    budget = budget_bytes(root)
    entries = _entries(root)
    used = sum(size for _, _, size in entries)
    for _, entry_dir, size in entries:
        fits_budget = used + needed <= budget
        fits_disk = _disk_free(root) - needed >= min_free_bytes()
        if fits_budget and fits_disk:
            return True
        if entry_dir.name == keep:
            continue
        if _evict(entry_dir, size, "least recently used"):
            used -= size
    return used + needed <= budget_bytes(root) and _disk_free(root) - needed >= min_free_bytes()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

class _BuildAborted(RuntimeError):
    pass


# Raised by the pipeline to pause/stop a stage; a cache build must never hide them.
_STOP_EXCEPTIONS = frozenset({"StageWaiting", "JobStopRequested"})


def build(part_uri: str, identity: dict[str, Any] | None = None) -> CachedPart | None:
    """Decompress ``part_uri`` once into a seekable tar + index. None if not possible."""
    identity = object_identity(part_uri, fresh=True) or identity
    if not identity:
        return None
    root = cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("part-cache directory %s is not writable; streaming reads continue", root)
        return None
    key = _key(part_uri)
    with _lock:
        thread_lock = _build_locks.setdefault(key, threading.Lock())
    with thread_lock:
        existing = lookup(part_uri, identity)
        if existing is not None:
            return existing
        lock_path = root / f".{key}.lock"
        with open(lock_path, "a+") as handle:
            # Another process/container may be building the same part: wait for it.
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                existing = lookup(part_uri, identity)
                if existing is not None:
                    return existing
                return _build_locked(part_uri, identity, root, key)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _build_locked(part_uri: str, identity: dict[str, Any], root: Path, key: str) -> CachedPart | None:
    import zstandard as zstd

    from app.services.storage import open_object_stream

    estimate = int(identity.get("size") or 0)
    if not make_room(estimate, keep=key):
        log.info(
            "part-cache skipped %s: %.1f GB would exceed the cache budget / free-space floor",
            part_uri, estimate / _GIB,
        )
        _count("fallbacks")
        return None
    entry_dir = root / key
    work = root / f".building-{key}-{os.getpid()}-{threading.get_ident()}"
    shutil.rmtree(work, ignore_errors=True)
    started = time.monotonic()
    stream = None
    try:
        work.mkdir(parents=True)
        tar_path = work / "part.tar"
        stream = open_object_stream(part_uri)
        if stream is None:
            raise _BuildAborted("object not readable")
        written = 0
        next_check = _FREE_CHECK_EVERY
        dctx = zstd.ZstdDecompressor()
        try:
            reader = dctx.stream_reader(stream, read_size=_CHUNK)
        except TypeError:
            reader = dctx.stream_reader(stream)
        with reader as zreader, open(tar_path, "wb", buffering=0) as out:
            while True:
                block = zreader.read(_CHUNK)
                if not block:
                    break
                out.write(block)
                written += len(block)
                if written >= next_check:
                    next_check = written + _FREE_CHECK_EVERY
                    if _disk_free(root) < min_free_bytes():
                        raise _BuildAborted("free space fell below EXTRACT_PART_CACHE_MIN_FREE_GB")
                    budget = budget_bytes(root)
                    if budget and written > budget:
                        raise _BuildAborted("part is larger than the cache budget")
        rows: list[list[Any]] = []
        # Seekable mode reads only headers and seeks past member data.
        with tarfile.open(tar_path, mode="r:") as tar:
            for member in tar:
                if member.isfile():
                    end = int(member.offset_data) + int(member.size or 0)
                    if end > written:
                        raise _BuildAborted(f"member {member.name} is truncated in the part")
                    rows.append([member.name.replace("\\", "/"), int(member.offset_data), int(member.size or 0)])
                if tar.members:
                    tar.members.clear()
        (work / "index.json").write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
        meta = {
            "version": VERSION,
            "part_uri": part_uri,
            "identity": identity,
            "tar_bytes": written,
            "members": len(rows),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "build_seconds": round(time.monotonic() - started, 1),
        }
        (work / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        shutil.rmtree(entry_dir, ignore_errors=True)
        os.replace(work, entry_dir)
    except BaseException as exc:
        shutil.rmtree(work, ignore_errors=True)
        if not isinstance(exc, Exception) or type(exc).__name__ in _STOP_EXCEPTIONS:
            raise  # stop/pause requests and interpreter exits are never swallowed
        with _lock:
            _failed_builds[part_uri] = time.monotonic()
        _count("build_failures")
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            log.warning("part-cache build of %s stopped: disk full; streaming reads continue", part_uri)
        else:
            log.warning("part-cache build of %s not used (%s); streaming reads continue", part_uri, exc)
        return None
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    part = _load_entry(part_uri, entry_dir)
    if part is None:
        return None
    _remember(part)
    _count("builds")
    elapsed = max(time.monotonic() - started, 0.001)
    log.info(
        "part-cache built %s: %s members, %.2f GB in %.0fs (%.0f MiB/s) — later reads seek directly",
        part_uri, f"{part.member_count:,}", part.tar_bytes / _GIB, elapsed, part.tar_bytes / elapsed / 1048576,
    )
    return part


def clear(part_uri: str | None = None) -> None:
    """Remove one cached part (or everything). Evidence in object storage is untouched."""
    root = cache_root()
    if part_uri:
        _forget(part_uri)
        with _lock:
            _identity_cache.pop(part_uri, None)
        _remove_entry(root / _key(part_uri))
        return
    with _lock:
        _index_cache.clear()
        _scan_counts.clear()
        _failed_builds.clear()
        _identity_cache.clear()
    for _, entry_dir, _ in _entries(root):
        _remove_entry(entry_dir)


__all__ = [
    "CachedPart",
    "release_space",
    "remove_idle",
    "acquire",
    "build",
    "budget_bytes",
    "cache_root",
    "clear",
    "enabled",
    "lookup",
    "make_room",
    "mode",
    "note_scan",
    "object_identity",
    "stats",
]
