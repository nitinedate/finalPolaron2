"""Synthetic-only smoke tests; require no personal chat data."""
from pathlib import Path
import os
import sqlite3
from unittest.mock import patch

from app.services.mobile_acquire.whatsapp_oneclick import decrypt_working_copy


def test_missing_existing_key_does_not_modify_working_copy(tmp_path: Path):
    (tmp_path / 'msgstore.db.crypt15').write_bytes(b'fictional backup')
    with patch('app.services.mobile_acquire.whatsapp_oneclick.shutil.which', return_value='/fake/wadecrypt'):
        result = decrypt_working_copy(tmp_path)
    assert result['status'] == 'key_unavailable'
    assert not (tmp_path / 'encrypted_backup.key').exists()
    assert not (tmp_path / 'msgstore.db').exists()


def test_success_only_for_verified_sqlite(tmp_path: Path):
    (tmp_path / 'msgstore.db.crypt15').write_bytes(b'fictional backup')
    sample = tmp_path.parent / 'synthetic.sqlite'
    c = sqlite3.connect(sample)
    c.execute('CREATE TABLE message (id integer primary key, body text)')
    c.execute("INSERT INTO message(body) VALUES ('synthetic')")
    c.commit()
    c.close()

    def fake_run(args, **kwargs):
        Path(args[3]).write_bytes(sample.read_bytes())
        return type('Proc', (), {'returncode': 0})()

    with patch('app.services.mobile_acquire.whatsapp_oneclick.shutil.which', return_value='/fake/wadecrypt'), \
         patch('app.services.mobile_acquire.whatsapp_oneclick.subprocess.run', side_effect=fake_run):
        result = decrypt_working_copy(tmp_path, hex_key='01' * 32)
    assert result['decrypted']
    assert (tmp_path / 'msgstore.db').read_bytes() == sample.read_bytes()
    assert (tmp_path / 'encrypted_backup.key').read_bytes() == bytes.fromhex('01' * 32)
    assert (tmp_path / 'whatsapp-decryption-audit.json').exists()


def test_recursive_android_post_extraction_scans_multiple_folders(tmp_path: Path):
    from app.services.mobile_acquire.whatsapp_oneclick import decrypt_extracted_android_tree
    for name in ('com.whatsapp', 'com.whatsapp.w4b'):
        folder = tmp_path / 'shared_storage' / 'Android' / 'media' / name / 'WhatsApp' / 'Databases'
        folder.mkdir(parents=True)
        (folder / 'msgstore.db.crypt15').write_bytes(b'fictional')
    result = decrypt_extracted_android_tree(tmp_path)
    assert result['found'] == 2
    assert result['decrypted'] == 0
    assert result['key_unavailable'] == 2
    assert result['device_modified'] is False
    assert all(not (tmp_path / row['directory'] / 'msgstore.db').exists() for row in result['items'])


def test_crypt14_is_detected_and_reports_missing_key(tmp_path: Path):
    from app.services.mobile_acquire.whatsapp_oneclick import decrypt_extracted_android_tree
    folder = tmp_path / 'shared_storage' / 'Android' / 'media' / 'com.whatsapp' / 'WhatsApp' / 'Databases'
    folder.mkdir(parents=True)
    (folder / 'msgstore.db.crypt14').write_bytes(b'synthetic data')
    result = decrypt_extracted_android_tree(tmp_path)
    assert result['found'] == 1
    assert result['items'][0]['format'] == 'crypt14'
    assert result['items'][0]['status'] == 'key_unavailable'
    assert not (folder / 'msgstore.db').exists()


def test_crypt15_and_crypt14_not_mixed_with_wrong_key_type(tmp_path: Path):
    folder = tmp_path / 'WhatsApp' / 'Databases'
    folder.mkdir(parents=True)
    (folder / 'msgstore.db.crypt15').write_bytes(b'synthetic data')
    (folder / 'key').write_bytes(b'X' * 158)
    with patch('app.services.mobile_acquire.whatsapp_oneclick.shutil.which', return_value='/fake/wadecrypt'):
        status = decrypt_working_copy(folder)
    assert status['status'] == 'key_unavailable'
    assert not (folder / 'msgstore.db').exists()
