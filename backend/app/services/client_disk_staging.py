"""Resumable, integrity-checked remote E01 staging before native TSK parsing.

Client evidence stays read-only on the workstation. EWF/RAW image segment bytes
are transferred by sequential reads to a *shared* server volume. Only after all
segments are fully copied and SHA-256 recorded are the job's evidence_files
paths changed atomically. This removes random network seeks from later TSK and
artifact parsing, and does not consume the extraction CPU admission slot.

The remote agent's frozen manifest + FUSE ETag checks guard against mixing
changed client bytes during a resumed transfer. Staged sources remain on the
server for forensic audit and should be governed by normal case retention.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db.sql_helpers import execute, fetchone
from app.services.client_evidence import is_client_evidence_path, split_client_path, to_container_path

_SEGMENT = re.compile(r'\.(?:e(?:\d{2}|(?!xe|wf)[a-z]{2})|\d{3}|dd|raw|vmdk|vhdx|aff4?)$', re.I)
_CHUNK = 8 * 1024 * 1024


def stage_root() -> Path:
    return Path(os.environ.get('REMOTE_DISK_STAGE_ROOT') or '/remote-disk-staging').resolve()


def needs_staging(files: list[dict], *, source_type: str = 'disk') -> bool:
    if str(source_type).lower() not in {'disk', 'disk_image', 'host_disk'}:
        return False
    if os.environ.get('REMOTE_DISK_STAGE_ENABLED', 'true').lower() in {'false', 'off', '0', 'no'}:
        return False
    return any(is_client_evidence_path(f.get('host_path')) and
               _SEGMENT.search(str(f.get('original_name') or '')) for f in files)


def _valid_job_id(job_id: str) -> str:
    if not re.fullmatch(r'[0-9a-fA-F-]{32,36}', str(job_id)):
        raise ValueError('Invalid disk job identifier')
    return str(job_id)


def _write_json_atomic(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.manifest-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(_CHUNK):
            hasher.update(block)
    return hasher.hexdigest()


def copy_segment_verified(source: Path, target: Path, *, expected_size: int, expected_small_sha256: str = '',
                          progress=None, known_digest: str = '') -> dict:
    """Copy a frozen segment safely, resume .partial after interrupted transport.

    A completed stage file is never reused based on size alone: checksum must
    match a prior verified manifest entry. Both stage and source sizes are
    checked, and the optional 32 MB-and-under source fingerprint is compared.
    """
    if expected_size < 0:
        raise ValueError('Invalid evidence segment size')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or source.is_symlink():
        raise ValueError('Cannot stage symlinked evidence')
    if target.parent.resolve() != target.parent.absolute():
        raise ValueError('Stage parent contains a symlink')
    # The registered size is an acquisition snapshot invariant. Check it even
    # if the previous server copy is known-good; the client may have changed.
    if int(source.stat().st_size) != expected_size:
        raise IOError(f'Source size changed: {source.name} (expected {expected_size} bytes)')
    if known_digest and target.is_file() and target.stat().st_size == expected_size:
        digest = _sha256(target)
        if digest == known_digest:
            if expected_small_sha256 and digest != expected_small_sha256:
                raise IOError(f'SHA-256 mismatch for source segment {source.name}')
            return {'bytes': expected_size, 'sha256': digest, 'resumed': True}
    # Do not trust an unmanifested stage even when its length happens to match.
    if target.exists():
        target.unlink()
    partial = target.with_name(target.name + '.partial')
    partial_size = partial.stat().st_size if partial.exists() else 0
    if partial_size > expected_size or partial.is_symlink():
        partial.unlink()
        partial_size = 0
    digest = hashlib.sha256()
    if partial_size:
        # Do not trust a previous truncated copy merely because the byte count
        # is unchanged. Compare every resumed byte with the frozen client
        # source. This costs network I/O only on interrupted transfers.
        with partial.open('rb') as previous, source.open('rb') as source_prefix:
            consumed = 0
            while consumed < partial_size:
                block = previous.read(min(_CHUNK, partial_size - consumed))
                remote = source_prefix.read(len(block))
                if not block or remote != block:
                    raise IOError(f'Interrupted copy differs from source: {source.name}; manual re-stage required')
                digest.update(block)
                consumed += len(block)
    with source.open('rb') as src, partial.open('ab') as output:
        if partial_size:
            src.seek(partial_size)
        copied = partial_size
        if progress:
            progress(copied)
        while copied < expected_size:
            block = src.read(min(_CHUNK, expected_size - copied))
            if not block:
                raise IOError(f'Evidence source ended early: {source.name} at {copied}/{expected_size}')
            output.write(block)
            digest.update(block)
            copied += len(block)
            if progress:
                progress(copied)
        output.flush()
        os.fsync(output.fileno())
    if int(source.stat().st_size) != expected_size or partial.stat().st_size != expected_size:
        raise IOError(f'Evidence segment changed during staging: {source.name}')
    actual = digest.hexdigest()
    if expected_small_sha256 and actual != expected_small_sha256:
        raise IOError(f'SHA-256 mismatch for source segment {source.name}')
    os.replace(partial, target)
    return {'bytes': expected_size, 'sha256': actual, 'resumed': partial_size > 0}


def _remote_plan(files: list[dict], root: Path) -> list[dict]:
    plan = []
    for row in files:
        raw = str(row.get('host_path') or '')
        if not is_client_evidence_path(raw) or not _SEGMENT.search(str(row.get('original_name') or '')):
            continue
        share_id, relative = split_client_path(raw)
        if not relative:
            raise ValueError('Remote evidence segment requires a file path')
        source = to_container_path(raw)
        target = root / share_id / relative
        if root not in target.parents:
            raise ValueError('Stage target escaped the evidence staging root')
        plan.append({'id': str(row['id']), 'original': raw, 'original_directory': raw.rsplit('/', 1)[0],
                     'source': source, 'target': target, 'size': int(row.get('size_bytes') or 0),
                     'registration_sha': str(row.get('sha256') or '')})
    if len(plan) > 2000:
        raise ValueError('Too many remote image segments in one job')
    return plan


def stage_remote_disk_evidence(db, job_id: str, files: list[dict], *, schema_name: str) -> list[dict]:
    """Stage all client disk images, then atomically switch registered paths.

    Multi-directory image grouping is retained; original folders and paths are
    saved in disk_source.remote_staging for chain-of-custody provenance.
    """
    from app.services.client_evidence.mount import ensure_mounted_for_paths
    from app.services.disk_build_log import write_disk_log, write_disk_log_committed
    from app.services.mobile_os import load_job_disk_source

    job_id = _valid_job_id(job_id)
    ds = load_job_disk_source(fetchone(db, 'SELECT disk_source FROM jobs WHERE id=:id', {'id': job_id}) or {})
    if not needs_staging(files, source_type=ds.get('source_type') or 'disk'):
        return files
    stage = stage_root() / job_id
    stage.mkdir(parents=True, exist_ok=True)
    if stage.is_symlink():
        raise ValueError('Remote disk staging directory must not be a symlink')
    import fcntl  # Disk workers use Linux; an advisory lock prevents duplicate transfer tasks.
    with (stage / '.transfer.lock').open('a+b') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        # Re-read after waiting for another extractor; its registration may be done.
        from app.db.sql_helpers import fetchall
        files = fetchall(db, 'SELECT * FROM evidence_files WHERE job_id=:id ORDER BY original_name', {'id': job_id})
        plan = _remote_plan(files, stage)
        if not plan:
            return files
        # Client shares are mounted read-only. All sources must be reachable.
        mount_activity = {'subphase': 'remote_mount', 'segment_count': len(plan),
                          'detail': 'waiting for frozen client share before sequential E01 transfer'}
        execute(db, """UPDATE jobs SET pipeline_progress=jsonb_set(
                    COALESCE(pipeline_progress, '{}'::jsonb), '{extraction_activity}',
                    CAST(:activity AS jsonb), true), updated_at=NOW() WHERE id=:id""",
                {'id': job_id, 'activity': json.dumps(mount_activity)})
        write_disk_log(db, job_id, f'Client image staging: {len(plan)} disk segments, sequential transfer before TSK parsing',
                       stage='remote_stage', metadata=mount_activity)
        db.commit()
        ensure_mounted_for_paths([step['original'] for step in plan])
        manifest_path = stage / 'sha256-manifest.json'
        manifest = {'job_id': job_id, 'algorithm': 'sha256', 'segments': {}}
        if manifest_path.is_file():
            try:
                saved = json.loads(manifest_path.read_text(encoding='utf-8'))
                if saved.get('job_id') == job_id and saved.get('algorithm') == 'sha256':
                    manifest['segments'] = saved.get('segments') or {}
            except (ValueError, OSError):
                pass
        verified = manifest['segments']
        remaining = 0
        for step in plan:
            saved = verified.get(step['id']) or {}
            dest = step['target']
            trusted = (saved.get('original') == step['original']
                       and int(saved.get('bytes') or -1) == step['size']
                       and saved.get('sha256')
                       and dest.is_file() and dest.stat().st_size == step['size'])
            if trusted:
                continue
            partial = dest.with_name(dest.name + '.partial')
            partial_bytes = (partial.stat().st_size if partial.is_file() and not partial.is_symlink() else 0)
            remaining += max(0, step['size'] - min(step['size'], partial_bytes))
        free = shutil.disk_usage(stage).free
        reserve = int(os.environ.get('REMOTE_DISK_STAGE_RESERVE_MB', '512')) * 1024 * 1024
        if remaining + reserve > free:
            raise OSError(f'Not enough server staging space: need {remaining / 2**30:.2f} GiB plus reserve, '
                          f'available {free / 2**30:.2f} GiB. Set REMOTE_DISK_STAGE_ROOT to a larger shared SSD.')
        lock = threading.Lock()
        progress = {step['id']: 0 for step in plan}
        last_log = [0.0]
        total_size = sum(step['size'] for step in plan)

        def on_segment_progress(segment: dict, count: int) -> None:
            with lock:
                progress[segment['id']] = count
                elapsed = time.monotonic()
                if elapsed - last_log[0] < 20:
                    return
                last_log[0] = elapsed
                progress_bytes = sum(progress.values())
                name = segment['source'].name
            # New session avoids sharing the caller's SQLAlchemy Session across threads.
            try:
                from app.db.session import firm_session
                with firm_session(schema_name) as callback_db:
                    activity = {'subphase': 'remote_staging', 'total_bytes': total_size,
                                'copied_bytes': progress_bytes, 'last_path': name,
                                'transfer_parallelism': len(progress)}
                    execute(callback_db, """UPDATE jobs SET pipeline_progress=jsonb_set(
                               COALESCE(pipeline_progress, '{}'::jsonb), '{extraction_activity}',
                               CAST(:activity AS jsonb), true), updated_at=NOW() WHERE id=:id""",
                            {'id': job_id, 'activity': json.dumps(activity)})
                    write_disk_log(callback_db, job_id,
                                   f'Client transfer: {progress_bytes:,}/{total_size:,} bytes staged; segment {name}',
                                   stage='remote_stage', metadata=activity)
            except Exception:
                # A telemetry failure must never cause source bytes to be discarded.
                pass

        def one(step: dict) -> tuple[dict, dict]:
            previous = verified.get(step['id']) or {}
            trusted = (str(previous.get('original') or '') == step['original']
                       and int(previous.get('bytes') or -1) == step['size'])
            registration_sha = step['registration_sha'] if step['size'] <= 32 * 1024 * 1024 else ''
            result = copy_segment_verified(step['source'], step['target'], expected_size=step['size'],
                        expected_small_sha256=registration_sha,
                        known_digest=str(previous.get('sha256') or '') if trusted else '',
                        progress=lambda count: on_segment_progress(step, count))
            on_segment_progress(step, step['size'])
            return step, result

        workers = max(1, min(3, int(os.environ.get('REMOTE_DISK_STAGE_THREADS', '2'))))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='remote-evidence-transfer') as pool:
            futures = {pool.submit(one, step): step for step in plan}
            for future in as_completed(futures):
                step, result = future.result()
                verified[step['id']] = {'original': step['original'], 'server_path': str(step['target']),
                                        'bytes': result['bytes'], 'sha256': result['sha256'],
                                        'verified_at': datetime.now(timezone.utc).isoformat()}
                _write_json_atomic(manifest_path, manifest)
                write_disk_log_committed(schema_name, job_id,
                    f'Verified staged segment {step["source"].name}: SHA-256 {result["sha256"][:16]}... '
                    f'({result["bytes"]:,} bytes)', stage='remote_stage')

        # All bytes present and verified: one atomic database transaction flips
        # every segment to the same shared server volume. No partial switch.
        replacements = {step['original_directory']: str(step['target'].parent) for step in plan}
        original_roster = list(ds.get('evidence_folders') or [])
        original_fingerprints = {step['id']: {
            'path': step['original'], 'registered_sha256_field': step['registration_sha'],
            'registered_size': step['size'],
        } for step in plan}
        for step in plan:
            result = execute(db, """UPDATE evidence_files SET host_path=:local, storage_uri=:uri, sha256=:actual_sha
                        WHERE job_id=:id AND id=CAST(:row_id AS uuid) AND host_path=:remote""",
                        {'local': str(step['target']), 'uri': 'file://' + str(step['target']),
                         'actual_sha': verified[step['id']]['sha256'],
                         'id': job_id, 'row_id': step['id'], 'remote': step['original']})
            if result.rowcount != 1:
                db.rollback()
                raise RuntimeError('Evidence segment registration changed while staging; no source paths were switched')
        # Convert source roster to the same server-local paths used by the DB.
        if original_roster:
            ds['original_client_evidence_folders'] = original_roster
            def local_folder(value: str) -> str:
                if not is_client_evidence_path(value):
                    return value
                share_id, rel = split_client_path(value)
                return str(stage / share_id / rel) if rel else str(stage / share_id)
            ds['evidence_folders'] = list(dict.fromkeys(local_folder(str(item)) for item in original_roster))
        else:
            ds['evidence_folders'] = list(dict.fromkeys(replacements.values()))
        # All published hashes now refer to actual staged file bytes, rather
        # than fingerprint_host_segment()'s path+size+mtime placeholder for
        # sources larger than 32 MiB. Preserve that registration value below.
        ds['segment_hashes'] = [verified[step['id']]['sha256'] for step in plan]
        ds['remote_staging'] = {'complete': True, 'location': str(stage), 'manifest': str(manifest_path),
                                'segment_count': len(plan), 'total_bytes': total_size,
                                'sha256_verified': True, 'originals_untouched': True,
                                'original_registration_fingerprints': original_fingerprints,
                                'registered_large_sha_was_metadata_fingerprint': True}
        ds['source_residency'] = 'client_snapshot_staged_to_server'
        ds['transport_mode'] = 'verified_server_staging'
        ds['evidence_location'] = 'server'
        ds['cleanup_policy'] = 'retain_server_snapshot_and_client_original'
        execute(db, 'UPDATE jobs SET disk_source=CAST(:ds AS jsonb), updated_at=NOW() WHERE id=:id',
                {'id': job_id, 'ds': json.dumps(ds)})
        write_disk_log(db, job_id,
                       f'Client disk image staged and SHA-256 verified ({len(plan)} segment(s)); native TSK will read local snapshots',
                       stage='remote_stage', metadata=ds['remote_staging'])
        db.commit()
        return fetchall(db, 'SELECT * FROM evidence_files WHERE job_id=:id ORDER BY original_name', {'id': job_id})
