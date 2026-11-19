"""Read-only, resumable client image staging regression tests."""
import hashlib
from pathlib import Path

import pytest

from app.services import client_disk_staging as staging


def test_detects_remote_disk_segments_but_not_mobile_evidence():
    files = [{'host_path': 'client-evidence://ce0123456789abcdef/Case/Evidence.E01',
              'original_name': 'Evidence.E01'}]
    assert staging.needs_staging(files, source_type='disk')
    assert not staging.needs_staging(files, source_type='mobile')
    assert not staging.needs_staging([{'host_path': '/local/Evidence.E01', 'original_name': 'Evidence.E01'}])


def test_stage_plan_preserves_share_and_original_subdirectory(tmp_path):
    files = [{'id': 'd1', 'host_path': 'client-evidence://ce0123456789abcdef/Folder-A/Evidence.E01',
              'original_name': 'Evidence.E01', 'size_bytes': 150},
             {'id': 'd2', 'host_path': 'client-evidence://ce0123456789abcdef/Folder-B/Evidence.E01',
              'original_name': 'Evidence.E01', 'size_bytes': 200}]
    plan = staging._remote_plan(files, tmp_path)
    assert len(plan) == 2
    assert plan[0]['target'] != plan[1]['target']
    assert plan[0]['target'] == tmp_path / 'ce0123456789abcdef' / 'Folder-A' / 'Evidence.E01'
    assert plan[1]['target'] == tmp_path / 'ce0123456789abcdef' / 'Folder-B' / 'Evidence.E01'


def test_resume_partial_transfer_verify_hash_and_preserve_source(tmp_path):
    source = tmp_path / 'original.E01'
    payload = (b'forensic-evidence\x00' * 65000)
    source.write_bytes(payload)
    destination = tmp_path / 'safe_stage' / 'original.E01'
    destination.parent.mkdir()
    (destination.parent / 'original.E01.partial').write_bytes(payload[:250_000])
    seen = []
    digest = hashlib.sha256(payload).hexdigest()
    result = staging.copy_segment_verified(source, destination, expected_size=len(payload),
                                           expected_small_sha256=digest, progress=seen.append)
    assert result['sha256'] == digest
    assert result['resumed'] is True
    assert seen[-1] == len(payload)
    assert destination.read_bytes() == payload
    assert source.read_bytes() == payload
    assert not Path(str(destination) + '.partial').exists()

    # Retrying with previously verified manifest re-hashes the staged copy.
    reused = staging.copy_segment_verified(source, destination, expected_size=len(payload), known_digest=digest)
    assert reused['sha256'] == digest


def test_source_size_mismatch_is_failure_not_fabricated_success(tmp_path):
    source = tmp_path / 'image.E01'
    source.write_bytes(b'content')
    with pytest.raises(IOError, match='Source size changed'):
        staging.copy_segment_verified(source, tmp_path / 'stage' / 'image.E01', expected_size=999)
    assert source.read_bytes() == b'content'


def test_wrong_small_file_digest_blocks_commit(tmp_path):
    source = tmp_path / 'original.E01'
    source.write_bytes(b'hello')
    target = tmp_path / 'stage' / 'original.E01'
    with pytest.raises(IOError, match='SHA-256 mismatch'):
        staging.copy_segment_verified(source, target, expected_size=5, expected_small_sha256='0' * 64)
    assert not target.exists()
    assert source.read_bytes() == b'hello'


def test_partial_resume_rejects_changed_same_size_source(tmp_path):
    src = tmp_path / 'source.E01'
    src.write_bytes(b'ABCDEFGHIJ')
    dst = tmp_path / 'out' / 'image.E01'
    dst.parent.mkdir()
    (dst.parent / 'image.E01.partial').write_bytes(b'ABCDE-----')
    with pytest.raises(IOError, match='Interrupted copy differs from source'):
        staging.copy_segment_verified(src, dst, expected_size=10)
    assert not dst.exists()
    assert src.read_bytes() == b'ABCDEFGHIJ'


def test_known_verified_stage_still_checks_frozen_source_size(tmp_path):
    src = tmp_path / 'source.E01'
    src.write_bytes(b'ABC')
    dst = tmp_path / 'out' / 'image.E01'
    dst.parent.mkdir()
    dst.write_bytes(b'ABC')
    src.write_bytes(b'ABCD')
    with pytest.raises(IOError, match='Source size changed'):
        staging.copy_segment_verified(src, dst, expected_size=3,
                                       known_digest=hashlib.sha256(b'ABC').hexdigest())
    assert dst.read_bytes() == b'ABC'
