"""Android-only, offline WhatsApp working-copy recovery with wa-crypt-tools.

Does not acquire keys from protected apps or manufacture keys for existing backups.
All writes occur in an examiner-designated working copy of an acquired case.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone

from app.services.mobile_forensic.whatsapp_crypt import parse_key_material


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def decrypt_working_copy(directory: Path, *, hex_key: str = '', _candidate_material: bytes | None = None, backup_format: str | None = None) -> dict:
    """Decrypt crypt15 using existing key; place verified output beside source.

    The caller must enforce tenant/case root authorization. Never operate on a
    device mount: this path is explicitly a working-copy directory.
    """
    directory = directory.resolve(strict=True)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('Working-copy directory does not exist')
    if backup_format is None:
        backup_format = next((fmt for fmt in ('crypt15', 'crypt14')
                              if (directory / ('msgstore.db.' + fmt)).is_file()), None)
    if backup_format not in ('crypt15', 'crypt14'):
        raise ValueError('No supported full WhatsApp database backup found')
    source = directory / ('msgstore.db.' + backup_format)
    output = directory / 'msgstore.db'
    key_filename = 'encrypted_backup.key' if backup_format == 'crypt15' else 'key'
    key_dest = directory / key_filename
    if not source.is_file() or source.is_symlink():
        raise ValueError('No ' + source.name + ' in selected working-copy directory')
    if output.exists() or output.is_symlink():
        raise ValueError('msgstore.db already exists; not replacing evidence or earlier output')
    executable = shutil.which('wadecrypt')
    if not executable:
        raise RuntimeError('wa-crypt-tools missing: install wa-crypt-tools in Android API worker')

    # Never cross-use crypt14 and crypt15 key types. Matching material is required.
    material = None
    key_origin = None
    if _candidate_material is not None:
        material, key_origin = _candidate_material, 'acquired_file'
    elif hex_key.strip() and backup_format == 'crypt15':
        cleaned = ''.join(c for c in hex_key if c not in ' \t\r\n:-')
        if len(cleaned) != 64 or any(c not in '0123456789abcdefABCDEF' for c in cleaned):
            raise ValueError('Expected the existing 64-character hexadecimal backup key')
        material = bytes.fromhex(cleaned)
        key_origin = 'examiner_supplied'
    else:
        candidates = [key_dest]
        # Existing working-copy extraction layout, scoped to THIS evidence tree.
        candidates.extend(sorted(directory.parent.glob('*/' + key_filename))[:128])
        candidates.extend(sorted(directory.parent.glob('*/files/' + key_filename))[:128])
        for candidate in candidates:
            try:
                if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 512:
                    continue
                data = candidate.read_bytes()
                parsed = parse_key_material(data)
                if parsed and ((backup_format == 'crypt15' and parsed.kind in ('raw32', 'hex64', 'java_backup_key32')) or
                               (backup_format == 'crypt14' and parsed.kind in ('keyfile158', 'java_keyfile158', 'key_payload131'))):
                    material, key_origin = data, 'acquired_file'
                    break
            except OSError:
                continue
    parsed = parse_key_material(material)
    supported = bool(parsed) and (
        (backup_format == 'crypt15' and parsed.kind in ('raw32', 'hex64', 'java_backup_key32')) or
        (backup_format == 'crypt14' and parsed.kind in ('keyfile158', 'java_keyfile158', 'key_payload131')))
    if not supported:
        return {'status': 'key_unavailable', 'format': backup_format, 'decrypted': False,
                'key_available': False, 'message': 'Existing matching WhatsApp key not found; cannot generate a replacement.'}

    with tempfile.TemporaryDirectory(prefix='polaron-wadecrypt-') as tmp:
        tmpdir = Path(tmp)
        private_key = tmpdir / key_filename
        temp_out = tmpdir / 'msgstore.db'
        private_key.write_bytes(material)
        private_key.chmod(0o600)
        try:
            process = subprocess.run([executable, str(private_key), str(source), str(temp_out)],
                                     timeout=600, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            return {'status': 'timeout', 'decrypted': False, 'key_available': True}
        if process.returncode != 0 or not temp_out.is_file():
            return {'status': 'key_mismatch_or_corrupt_backup', 'decrypted': False, 'key_available': True}
        with temp_out.open('rb') as verification_stream:
            magic = verification_stream.read(16)
        if magic != b'SQLite format 3\x00':
            return {'status': 'not_sqlite', 'decrypted': False, 'key_available': True}
        connection = sqlite3.connect(f'file:{temp_out.as_posix()}?mode=ro', uri=True)
        try:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                return {'status': 'sqlite_integrity_failed', 'decrypted': False, 'key_available': True}
        finally:
            connection.close()
        # This creates derived evidence in the working copy, not on the device.
        # Never overwrite a pre-existing key, and never create a key until validated.
        if not key_dest.exists():
            fd = os.open(key_dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(material)
        # Atomic destination publishing even when /tmp and evidence are different mounts.
        # Stage within output filesystem then publish with an exclusive hard link.
        fd, staged = tempfile.mkstemp(prefix='.msgstore-', dir=directory)
        try:
            os.chmod(staged, 0o600)
            with os.fdopen(fd, 'wb') as target, temp_out.open('rb') as source_stream:
                shutil.copyfileobj(source_stream, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.link(staged, output)
        finally:
            Path(staged).unlink(missing_ok=True)
    report = {'status': 'decrypted', 'format': backup_format, 'decrypted': True, 'key_available': True,
              'key_origin': key_origin, 'source': source.name, 'output': output.name,
              'key_file': key_dest.name, 'source_sha256': _digest(source),
              'output_sha256': _digest(output), 'key_sha256': _digest(key_dest),
              'tool': 'wa-crypt-tools/wadecrypt',
              'timestamp_utc': datetime.now(timezone.utc).isoformat(),
              'source_unchanged': True, 'device_modified': False}
    audit = directory / 'whatsapp-decryption-audit.json'
    if not audit.exists():
        fd = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2)
    return report


def decrypt_extracted_android_tree(root: Path, *, hex_key: str = '', extra_key_paths=()) -> dict:
    """Offline post-extraction scan of Android case data only.

    No ADB/device operations. Results are stored within the examiner's acquisition
    copy and cannot be mistaken for original device files. Does not synthesize keys.
    """
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('Invalid acquired Android extraction root')
    backups = []
    existing_keys = {'crypt14': [], 'crypt15': []}
    # Limit traversal to the extracted tree; reject symlinks, no outside walks.
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        folder = Path(base)
        for fmt in ('crypt15', 'crypt14'):
            filename = 'msgstore.db.' + fmt
            if filename in files and not (folder / filename).is_symlink():
                backups.append((folder, fmt))
            key_filename = 'encrypted_backup.key' if fmt == 'crypt15' else 'key'
            if key_filename in files:
                key = folder / key_filename
                if not key.is_symlink() and key.stat().st_size <= 512:
                    existing_keys[fmt].append(key)
    # External intake candidates permitted only if already acquired beneath case root.
    for raw in extra_key_paths or ():
        try:
            candidate = Path(raw).resolve(strict=True)
            if candidate.is_relative_to(root) and candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= 512:
                if candidate.name == 'encrypted_backup.key':
                    existing_keys['crypt15'].append(candidate)
                elif candidate.name == 'key':
                    existing_keys['crypt14'].append(candidate)
        except (OSError, ValueError, TypeError):
            continue
    # Never include plaintext keys in audit or exceptions.
    results = []
    for folder, fmt in sorted(set(backups)):
        relative = str(folder.relative_to(root))
        if (folder / 'msgstore.db').exists():
            status = {'status': 'already_exists', 'decrypted': False, 'key_available': False, 'format': fmt}
        else:
            status = {'status': 'key_unavailable', 'decrypted': False, 'key_available': False, 'format': fmt}
            # Prefer folder-local key, then previously acquired evidence keys.
            candidates = [folder / ('encrypted_backup.key' if fmt == 'crypt15' else 'key')] + existing_keys[fmt]
            if hex_key.strip() and fmt == 'crypt15':
                try:
                    status = decrypt_working_copy(folder, hex_key=hex_key, backup_format=fmt)
                except (ValueError, RuntimeError, OSError):
                    status = {'status': 'decryption_failed', 'decrypted': False, 'key_available': False}
            if not status.get('decrypted'):
                for key_path in dict.fromkeys(candidates):
                    try:
                        if key_path.is_symlink() or not key_path.is_file() or key_path.stat().st_size > 512:
                            continue
                        material = key_path.read_bytes()
                        parsed = parse_key_material(material)
                        if not parsed or not ((fmt == 'crypt15' and parsed.kind in ('raw32', 'hex64', 'java_backup_key32')) or
                                              (fmt == 'crypt14' and parsed.kind in ('keyfile158', 'java_keyfile158', 'key_payload131'))):
                            continue
                        # 64-character text is accepted by the existing helper;
                        # binary key material can be staged as a per-candidate file.
                        status = decrypt_working_copy(folder, _candidate_material=material, backup_format=fmt)
                        if status.get('decrypted'):
                            break
                    except (ValueError, RuntimeError, OSError):
                        status = {'status': 'decryption_failed', 'decrypted': False, 'key_available': True}
        results.append({'directory': relative, 'format': fmt, **status})
    summary = {'platform': 'android', 'processing': 'offline_post_extraction',
               'device_modified': False, 'found': len(backups),
               'decrypted': sum(bool(v.get('decrypted')) for v in results),
               'key_unavailable': sum(v['status'] == 'key_unavailable' for v in results),
               'items': results}
    report = root / 'whatsapp-post-extraction.json'
    # Refresh the outcome on reprocessing instead of leaving stale missing-key status.
    fd, staged_report = tempfile.mkstemp(prefix='.whatsapp-report-', dir=root)
    try:
        os.chmod(staged_report, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(summary, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged_report, report)
    finally:
        Path(staged_report).unlink(missing_ok=True)
    return summary
