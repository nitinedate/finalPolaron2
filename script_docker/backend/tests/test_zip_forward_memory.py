"""Zip shard reads must stay bounded.

A logical iPhone zip is one archive of ~180k members. Unbounded deflate and
8 MiB virtiofs I/O both surface as ``[Errno 12] Cannot allocate memory``
after the shard has already packed tens of thousands of small photos.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.services.extract_shard_v45 import _BoundedWriter
from app.services.extracted_disk import _transient_extract_io
from app.services.virtual_disk import ZipForwardReader


def test_deflated_member_is_yielded_in_capped_chunks(tmp_path: Path):
    payload = b"\0" * 2_000_000
    archive = tmp_path / "logical.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readable_artifacts/media/a.jpg", b"jpg-a", compress_type=zipfile.ZIP_STORED)
        zf.writestr("readable_artifacts/media/zeros.bin", payload, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("readable_artifacts/media/b.jpg", b"jpg-b", compress_type=zipfile.ZIP_STORED)
        offsets = {info.filename: int(info.header_offset) for info in zf.infolist()}

    reader = ZipForwardReader(archive)
    try:
        cap = 64 * 1024
        chunks = list(reader.iter_member(offsets["readable_artifacts/media/zeros.bin"], cap))
        assert b"".join(chunks) == payload
        assert chunks
        assert max(len(chunk) for chunk in chunks) <= cap
        assert b"".join(reader.iter_member(offsets["readable_artifacts/media/b.jpg"], cap)) == b"jpg-b"
    finally:
        reader.close()


def test_errno_12_read_retries_smaller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    archive = tmp_path / "logical.zip"
    body = b"photo-bytes" * 1000
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readable_artifacts/media/5005.JPG", body, compress_type=zipfile.ZIP_STORED)
        offset = int(zf.infolist()[0].header_offset)

    monkeypatch.setattr("app.services.virtual_disk.time.sleep", lambda _s: None)
    reader = ZipForwardReader(archive)
    real = reader._f
    calls: list[int] = []

    class _Flaky:
        def read(self, n: int) -> bytes:
            calls.append(n)
            if n >= 1024 * 1024:
                raise OSError(12, "Cannot allocate memory")
            return real.read(n)

        def seek(self, pos: int, whence: int = 0) -> int:
            return real.seek(pos, whence)

        def fileno(self) -> int:
            return real.fileno()

        def close(self) -> None:
            real.close()

    reader._f = _Flaky()
    try:
        assert b"".join(reader.iter_member(offset, 1024 * 1024)) == body
    finally:
        reader.close()
    assert any(n >= 1024 * 1024 for n in calls)
    assert reader._read_cap < 1024 * 1024


def test_bounded_writer_splits_large_packs():
    parts: list[bytes] = []

    class _Raw:
        def write(self, data) -> int:
            parts.append(bytes(data))
            return len(data)

    chunk = 64 * 1024
    writer = _BoundedWriter(_Raw(), chunk)
    assert writer.write(b"x" * (chunk + 100)) == chunk + 100
    assert [len(part) for part in parts] == [chunk, 100]


def test_cannot_allocate_memory_is_retried_as_transient_io():
    assert _transient_extract_io(OSError(12, "Cannot allocate memory"))
    assert not _transient_extract_io(OSError(2, "No such file"))
