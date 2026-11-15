"""Opt-in Android WhatsApp re-backup after an offline key-missing result.

WARNING: This operation alters the evidence device. The flag and examiner
authorization must be recorded before enabling it. No root or privilege bypass.
UI automation is best-effort and fails closed when key text is not accessible.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


ANDROID_MEDIA = '/sdcard/Android/media/com.whatsapp'
_HEX64 = re.compile(r'(?<![0-9a-fA-F])(?:[0-9a-fA-F]{4}[\s\-:]*){15}[0-9a-fA-F]{4}(?![0-9a-fA-F])')
_BOUNDS = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')


class DeviceRecoveryUnavailable(Exception):
    pass


def _run(adb: str, serial: str, *args: str, timeout: int = 50) -> str:
    if not serial or serial.startswith('-'):
        raise DeviceRecoveryUnavailable('invalid_adb_serial')
    result = subprocess.run([adb, '-s', serial, *args], capture_output=True,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        raise DeviceRecoveryUnavailable('adb_command_failed')
    return result.stdout


def _screen(adb: str, serial: str) -> list[dict]:
    # Android UI automation may create a temporary XML file on the device.
    remote = '/sdcard/Download/.polaron_whatsapp_ui.xml'
    try:
        _run(adb, serial, 'shell', 'uiautomator', 'dump', remote, timeout=35)
        xml = _run(adb, serial, 'exec-out', 'cat', remote, timeout=20)
    finally:
        try:
            _run(adb, serial, 'shell', 'rm', '-f', remote, timeout=15)
        except Exception:
            pass
    nodes = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DeviceRecoveryUnavailable('ui_hierarchy_unavailable') from exc
    for node in root.iter('node'):
        bounds = _BOUNDS.fullmatch(node.get('bounds', ''))
        if bounds:
            x1, y1, x2, y2 = map(int, bounds.groups())
            nodes.append({'text': node.get('text', ''), 'description': node.get('content-desc', ''),
                          'resource': node.get('resource-id', ''), 'x': (x1 + x2)//2, 'y': (y1 + y2)//2,
                          'clickable': node.get('clickable') == 'true'})
    return nodes


def _tap(adb: str, serial: str, patterns: tuple[str, ...], *, tries: int = 3) -> None:
    for _ in range(tries):
        nodes = _screen(adb, serial)
        for pattern in patterns:
            match = re.compile(pattern, re.IGNORECASE)
            for node in nodes:
                if match.search(node['text']) or match.search(node['description']):
                    _run(adb, serial, 'shell', 'input', 'tap', str(node['x']), str(node['y']))
                    time.sleep(1.2)
                    return
        time.sleep(1)
    raise DeviceRecoveryUnavailable('ui_control_not_found')


def _capture_key(adb: str, serial: str) -> str:
    # The device may intentionally hide the key from UIAutomator. Do not
    # screenshot/OCR or invent a key when the platform blocks access.
    nodes = _screen(adb, serial)
    all_text = ' '.join((n['text'] + ' ' + n['description']) for n in nodes)
    for match in _HEX64.finditer(all_text):
        candidate = re.sub(r'[^0-9a-fA-F]', '', match.group(0))
        if len(candidate) == 64:
            return candidate.lower()
    raise DeviceRecoveryUnavailable('device_generated_key_not_exposed_to_automation')


def _remote_crypt15_signature(adb: str, serial: str) -> str | None:
    remote = ANDROID_MEDIA + '/WhatsApp/Databases/msgstore.db.crypt15'
    try:
        value = _run(adb, serial, 'shell', 'stat', '-c', '%s:%Y', remote, timeout=15).strip()
        return value if re.fullmatch(r'\d+:\d+', value) else None
    except DeviceRecoveryUnavailable:
        return None


def _await_new_crypt15(adb: str, serial: str, previous: str | None, *, max_polls: int = 90) -> None:
    # Require a fresh, nonempty and stable file before pulling. A snapshot can
    # otherwise catch a database while WhatsApp is still writing the backup.
    last = None
    stable = 0
    for _ in range(max_polls):
        time.sleep(3)
        current = _remote_crypt15_signature(adb, serial)
        if current and current != previous and int(current.split(':', 1)[0]) > 4096:
            stable = stable + 1 if current == last else 1
            if stable >= 3:
                return
        else:
            stable = 0
        last = current
    raise DeviceRecoveryUnavailable('new_crypt15_not_stable_or_not_created')


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024*1024), b''):
            h.update(data)
    return h.hexdigest()


def attempt_authorized_rebackup(*, adb: str, serial: str, acquisition_root: Path,
                                examiner_authorized: bool = False) -> dict:
    """Attempt new crypt15 generation, never claiming to decrypt old keyless data.

    Requires an explicit flag AND authorization; produces a fresh working-copy
    branch, preserving original acquisition and its hashes.
    """
    report = {'status': 'not_attempted', 'device_modified': False, 'new_backup': False,
              'source': 'android_whatsapp_authorized_rebackup',
              'timestamp_utc': datetime.now(timezone.utc).isoformat()}
    if os.getenv('POLARON_WHATSAPP_ALLOW_DEVICE_MUTATION', '').lower() not in ('1', 'true', 'yes'):
        report['status'] = 'device_modification_disabled'
        return report
    if not examiner_authorized:
        report['status'] = 'authorization_required'
        return report
    root = Path(acquisition_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('invalid_acquisition_root')
    branch = root / 'whatsapp_authorized_rebackup'
    if branch.exists():
        report['status'] = 'rebackup_branch_exists'
        return report
    branch.mkdir(mode=0o700)
    # The UI is only automated by accessible control labels, never blind screen coordinates.
    # Supported on English language versions; changed WhatsApp layouts fail closed.
    report['device_modified'] = True
    try:
        _run(adb, serial, 'shell', 'monkey', '-p', 'com.whatsapp', '1')
        time.sleep(2)
        _tap(adb, serial, (r'^(more options|more)$', r'options'))
        _tap(adb, serial, (r'^settings$',))
        _tap(adb, serial, (r'^chats$',))
        _tap(adb, serial, (r'^chat backup$', r'^backup$'))
        # First: create a fresh local backup as requested.
        _tap(adb, serial, (r'^back up$', r'^backup now$'))
        # Cannot infer completion from a fixed delay; require an on-screen
        # completion indicator before changing the encryption setting.
        backup_done = False
        saw_busy = False
        for _ in range(45):
            time.sleep(2)
            nodes = _screen(adb, serial)
            text = ' '.join(n['text'] for n in nodes)
            if re.search(r'backing up|preparing|uploading|creating backup', text, re.I):
                saw_busy = True
                continue
            if saw_busy and re.search(r'last backup|local backup|back up', text, re.I):
                backup_done = True
                break
        if not backup_done:
            raise DeviceRecoveryUnavailable('backup_completion_not_verifiable')
        _tap(adb, serial, (r'end.to.end encrypted backup',))
        # Existing E2EE setup may have a different key; do not turn it off.
        nodes = _screen(adb, serial)
        screen_text = ' '.join(n['text'] for n in nodes)
        if any(re.search(r'^(turn off|change password|change key)$', n['text'], re.I) for n in nodes):
            raise DeviceRecoveryUnavailable('existing_e2ee_configuration_not_modified')
        _tap(adb, serial, (r'^turn on$',))
        _tap(adb, serial, (r'64.digit encryption key', r'use 64.digit key'))
        _tap(adb, serial, (r'^generate', r'^create key'))
        secret = _capture_key(adb, serial)
        # Complete configuration only after acquiring the exact key.
        _tap(adb, serial, (r'^continue$', r'^i saved', r'^create$', r'^done$'))
        report['key_captured'] = True
        # Backup again with the newly configured key. The old crypt14
        # acquisition is left intact; only the fresh crypt15 is decrypted.
        previous_crypt15 = _remote_crypt15_signature(adb, serial)
        _tap(adb, serial, (r'^back up$', r'^backup now$'))
        _await_new_crypt15(adb, serial, previous_crypt15)
        # The device's shared data is copied into an isolated output branch.
        _run(adb, serial, 'pull', ANDROID_MEDIA, str(branch), timeout=900)
        files = sorted(branch.rglob('msgstore.db.crypt15'))
        if not files:
            raise DeviceRecoveryUnavailable('new_crypt15_not_found_after_pull')
        from app.services.mobile_acquire.whatsapp_oneclick import decrypt_working_copy
        target = files[0].parent
        result = decrypt_working_copy(target, hex_key=secret, backup_format='crypt15')
        if not result.get('decrypted'):
            raise DeviceRecoveryUnavailable('new_backup_key_or_integrity_validation_failed')
        # Persist key.txt only for verified matches, in the derived working copy.
        key_text = target / 'key.txt'
        fd = os.open(key_text, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as file:
            file.write(secret + '\n')
        report.update({'status': 'decrypted_new_backup', 'new_backup': True,
                       'encrypted_source_sha256': _sha256(files[0]),
                       'decrypted_sha256': _sha256(target / 'msgstore.db'),
                       'key_filename': str(key_text.relative_to(root)),
                       'output_directory': str(target.relative_to(root))})
    except (DeviceRecoveryUnavailable, subprocess.TimeoutExpired, OSError) as exc:
        report['status'] = 'rebackup_requires_examiner_review'
        report['reason'] = str(exc) if isinstance(exc, DeviceRecoveryUnavailable) else type(exc).__name__
    finally:
        (branch / 'device-rebackup-audit.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report
