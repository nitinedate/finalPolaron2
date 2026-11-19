"""V1.28.0 — seekable local part cache: same evidence bytes, far fewer shard scans.

Every read through ``tar_cache`` must return exactly what the original streaming
reader returns (order, bytes, caps, repeats, size/SHA-256 verdicts, missing
members), whether the part cache is off, being built, or already present.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import time
from pathlib import Path

import pytest

zstd = pytest.importorskip("zstandard")


def make_part(path: Path, members: list[tuple[str, bytes]], *, dirs: tuple[str, ...] = ("docs",)) -> str:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w|") as tar:
        for d in dirs:
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1_700_000_000
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(zstd.ZstdCompressor(level=1).compress(raw.getvalue()))
    return f"file://{path}"


@pytest.fixture
def members():
    rnd = __import__("random").Random(7)
    big = bytes(rnd.getrandbits(8) for _ in range(256 * 1024)) * 80  # 20 MiB, > the 8 MiB read chunk
    out = [
        ("docs/a.txt", b"alpha\n" * 100),
        ("Android/data/com.whatsapp/databases/msgstore.db", b"SQLite format 3\x00" + os.urandom(70_000)),
        ("Android/data/com.whatsapp/databases/msgstore.db-wal", os.urandom(33_000)),
        ("DCIM/Camera/IMG_0001.jpg", big),
        ("empty.bin", b""),
        ("dup/same.txt", b"first copy"),
        ("Notizen über München.txt", "Grüße 😀".encode()),
    ]
    out += [(f"bulk/file_{i:04d}.dat", os.urandom(rnd.randint(1, 9000))) for i in range(300)]
    out.append(("dup/same.txt", b"second copy, longer"))
    return out


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    from app.services import part_cache, tar_cache

    root = tmp_path / "cache"
    monkeypatch.setenv("EXTRACT_PART_CACHE_DIR", str(root))
    monkeypatch.setenv("EXTRACT_PART_CACHE_MIN_FREE_GB", "1")
    monkeypatch.delenv("EXTRACT_PART_CACHE_GB", raising=False)
    part_cache.clear()
    tar_cache.clear_tar_cache()
    yield root
    part_cache.clear()
    tar_cache.clear_tar_cache()


def run(uri, paths, **kw):
    from app.services import tar_cache

    tar_cache.clear_tar_cache()  # the small in-memory cache must not hide a difference
    return list(tar_cache.iter_files_from_part(uri, set(paths), **kw))


QUERIES = [
    ({"docs/a.txt"}, {}),
    ({"DCIM/Camera/IMG_0001.jpg", "empty.bin", "missing/nowhere.txt"}, {}),
    ({"DCIM/Camera/IMG_0001.jpg", "docs/a.txt"}, {"max_bytes": 4096}),
    ({"DCIM/Camera/IMG_0001.jpg", "bulk/file_0007.dat", "empty.bin"}, {"max_bytes_by_path": {"DCIM/Camera/IMG_0001.jpg": 10, "empty.bin": 0}}),
    ({"dup/same.txt"}, {}),
    ({"dup/same.txt", "docs/a.txt"}, {"repeat_by_path": {"dup/same.txt": 2}}),
    ({"dup/same.txt"}, {"repeat_by_path": {"dup/same.txt": 3}}),
    ({"Notizen über München.txt", "docs"}, {}),
    ({f"bulk/file_{i:04d}.dat" for i in range(0, 300, 7)}, {}),
]


def _verify_queries(members):
    data = dict(members)  # last duplicate wins in dict(), so compute first-copy hashes explicitly
    first = {}
    for name, blob in members:
        first.setdefault(name, blob)
    msg = "Android/data/com.whatsapp/databases/msgstore.db"
    img = "DCIM/Camera/IMG_0001.jpg"
    return [
        ({msg, img}, {
            "verify_sha256_by_path": {msg: hashlib.sha256(first[msg]).hexdigest(), img: hashlib.sha256(first[img]).hexdigest()},
            "verify_size_by_path": {msg: len(first[msg]), img: len(first[img])},
            "max_bytes_by_path": {msg: len(first[msg]), img: 4096},
        }),
        ({msg}, {"verify_sha256_by_path": {msg: "0" * 64}}),
        ({img}, {"verify_size_by_path": {img: 5}}),
        ({"empty.bin"}, {"verify_sha256_by_path": {"empty.bin": hashlib.sha256(b"").hexdigest()}, "verify_size_by_path": {"empty.bin": 0}}),
    ]


def test_cached_reads_are_identical_to_streaming_reads(tmp_path, members, cache_env, monkeypatch):
    from app.services import part_cache

    uri = make_part(tmp_path / "part-00000.tar.zst", members)
    queries = QUERIES + _verify_queries(members)
    monkeypatch.setenv("EXTRACT_PART_CACHE", "off")
    expected = [run(uri, paths, **kw) for paths, kw in queries]
    assert not cache_env.exists() or not any(cache_env.iterdir())

    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    before = part_cache.stats()
    got = [run(uri, paths, **kw) for paths, kw in queries]
    assert got == expected
    after = part_cache.stats()
    assert after["builds"] - before["builds"] == 1, "the part is decompressed once"
    assert after["hits"] - before["hits"] == len(queries)
    # Spot-check a few semantics so "identical" is identical to something meaningful.
    assert expected[4] == [("dup/same.txt", b"first copy")]
    assert expected[5][:2] == [("docs/a.txt", b"alpha\n" * 100), ("dup/same.txt", b"first copy")] or \
        ("dup/same.txt", b"second copy, longer") in expected[5]
    assert expected[6].count(("dup/same.txt", None)) == 1
    assert ("missing/nowhere.txt", None) in expected[1]
    assert ("docs", None) in expected[7]
    verified = dict(got[len(QUERIES)])
    assert verified["DCIM/Camera/IMG_0001.jpg"] == dict(members)["DCIM/Camera/IMG_0001.jpg"][:4096]
    assert got[len(QUERIES) + 1] == [("Android/data/com.whatsapp/databases/msgstore.db", None)]
    assert got[len(QUERIES) + 2] == [("DCIM/Camera/IMG_0001.jpg", None)]


def test_spilled_reads_are_identical(tmp_path, members, cache_env, monkeypatch):
    from app.services import tar_cache

    uri = make_part(tmp_path / "part-00001.tar.zst", members)
    wanted = {"DCIM/Camera/IMG_0001.jpg", "dup/same.txt", "empty.bin", "nope.bin"}

    def spill():
        out = []
        for name, temp in tar_cache.iter_spilled_files_from_part(uri, set(wanted), directory=str(tmp_path)):
            out.append((name, Path(temp).read_bytes()))
            os.unlink(temp)
        return out

    monkeypatch.setenv("EXTRACT_PART_CACHE", "off")
    expected = spill()
    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    assert spill() == expected
    assert sorted(n for n, _ in expected) == ["DCIM/Camera/IMG_0001.jpg", "dup/same.txt", "empty.bin"]


def test_auto_mode_builds_on_the_second_scan_and_then_seeks(tmp_path, members, cache_env, monkeypatch):
    from app.services import part_cache, storage

    monkeypatch.setenv("EXTRACT_PART_CACHE", "auto")
    uri = make_part(tmp_path / "part-00002.tar.zst", members)
    opened = []
    real_open = storage.open_object_stream
    monkeypatch.setattr(storage, "open_object_stream", lambda u: opened.append(u) or real_open(u))
    from app.services import tar_cache
    monkeypatch.setattr(tar_cache, "open_object_stream", storage.open_object_stream)

    assert run(uri, {"docs/a.txt"}) == [("docs/a.txt", b"alpha\n" * 100)]
    assert part_cache.lookup(uri) is None, "a single pass does not pay for a cache build"
    run(uri, {"bulk/file_0299.dat"})
    assert part_cache.lookup(uri) is not None
    streams_after_build = len(opened)
    for i in range(0, 300, 3):
        run(uri, {f"bulk/file_{i:04d}.dat"})
    assert len(opened) == streams_after_build, "later reads never stream the shard again"


def test_a_re_extracted_part_is_never_served_from_the_old_copy(tmp_path, members, cache_env, monkeypatch):
    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    path = tmp_path / "part-00003.tar.zst"
    uri = make_part(path, members)
    assert run(uri, {"docs/a.txt"}) == [("docs/a.txt", b"alpha\n" * 100)]
    time.sleep(0.01)
    make_part(path, [("docs/a.txt", b"re-extracted content")])
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert run(uri, {"docs/a.txt", "dup/same.txt"}) == [("docs/a.txt", b"re-extracted content"), ("dup/same.txt", None)]


def test_budget_and_free_space_limits_fall_back_to_streaming(tmp_path, members, cache_env, monkeypatch):
    from app.services import part_cache

    uri = make_part(tmp_path / "part-00004.tar.zst", members)
    monkeypatch.setenv("EXTRACT_PART_CACHE", "off")
    expected = run(uri, {"DCIM/Camera/IMG_0001.jpg", "docs/a.txt"})
    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    monkeypatch.setenv("EXTRACT_PART_CACHE_GB", "0.000001")  # ~1 KB budget
    assert run(uri, {"DCIM/Camera/IMG_0001.jpg", "docs/a.txt"}) == expected
    assert part_cache.lookup(uri) is None
    monkeypatch.delenv("EXTRACT_PART_CACHE_GB")
    monkeypatch.setenv("EXTRACT_PART_CACHE_MIN_FREE_GB", "100000000")  # floor above any disk
    part_cache.clear()
    assert run(uri, {"DCIM/Camera/IMG_0001.jpg", "docs/a.txt"}) == expected
    assert part_cache.lookup(uri) is None


def test_damaged_and_truncated_parts_behave_exactly_as_before(tmp_path, members, cache_env, monkeypatch):
    from app.services import part_cache

    wanted = {"docs/a.txt", "bulk/file_0150.dat", "bulk/file_0299.dat", "dup/same.txt"}
    # Damaged bytes inside stored (incompressible) data still decompress: both
    # readers return the same (damaged) bytes, and callers' SHA-256 checks catch it.
    path = tmp_path / "part-00005.tar.zst"
    make_part(path, members)
    blob = bytearray(path.read_bytes())
    blob[len(blob) // 2: len(blob) // 2 + 64] = b"\x00" * 64
    path.write_bytes(bytes(blob))
    uri = f"file://{path}"
    monkeypatch.setenv("EXTRACT_PART_CACHE", "off")
    expected = run(uri, wanted)
    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    assert run(uri, wanted) == expected

    # A truncated part cannot be decompressed to the end: it is never cached, and
    # the streaming reader yields what it always yielded (members before the cut).
    cut = tmp_path / "part-00008.tar.zst"
    make_part(cut, members)
    cut.write_bytes(cut.read_bytes()[: int(cut.stat().st_size * 0.6)])
    cut_uri = f"file://{cut}"
    monkeypatch.setenv("EXTRACT_PART_CACHE", "off")
    expected_cut = run(cut_uri, wanted)
    assert ("bulk/file_0299.dat", None) in expected_cut
    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    assert run(cut_uri, wanted) == expected_cut
    assert part_cache.lookup(cut_uri) is None
    assert not [p for p in cache_env.iterdir() if p.name.startswith(".building-")]


def test_least_recently_used_part_is_evicted_to_fit_the_budget(tmp_path, cache_env, monkeypatch):
    from app.services import part_cache

    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    blobs = [(f"f{i}.bin", os.urandom(400_000)) for i in range(3)]
    first = make_part(tmp_path / "p1.tar.zst", blobs)
    second = make_part(tmp_path / "p2.tar.zst", blobs)
    run(first, {"f0.bin"})
    assert part_cache.lookup(first) is not None
    one_entry = sum(f.stat().st_size for f in (cache_env / part_cache._key(first)).iterdir())
    monkeypatch.setenv("EXTRACT_PART_CACHE_GB", str((one_entry * 1.5) / 1024 ** 3))
    run(second, {"f1.bin"})
    assert part_cache.lookup(second) is not None
    assert part_cache.lookup(first) is None
    assert run(first, {"f2.bin"}) == [("f2.bin", dict(blobs)["f2.bin"])]


def test_stop_requests_during_a_build_are_not_swallowed(tmp_path, members, cache_env, monkeypatch):
    from app.services import part_cache, storage

    class StageWaiting(RuntimeError):
        pass

    uri = make_part(tmp_path / "part-00006.tar.zst", members)
    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")

    def stopping(_uri):
        raise StageWaiting("paused")

    monkeypatch.setattr(storage, "open_object_stream", stopping)
    with pytest.raises(StageWaiting):
        part_cache.build(uri)
    assert not [p for p in cache_env.iterdir() if p.name.startswith(".building-")]


def test_mobile_direct_reader_gets_verified_bytes_through_the_cache(tmp_path, members, cache_env, monkeypatch):
    from app.services.mobile_forensic.direct_part_reader import read_claimed_member
    from app.services.tar_cache import read_file_from_part

    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    uri = make_part(tmp_path / "part-00007.tar.zst", members)
    name = "Android/data/com.whatsapp/databases/msgstore.db"
    blob = dict(members)[name]
    row = {"path": name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest(), "meta": {"extracted_part_uri": uri}}
    assert read_claimed_member(row, name, None, read_file_from_part) == (True, blob)
    bad = dict(row, sha256="f" * 64)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        read_claimed_member(bad, name, None, read_file_from_part)


def test_faster_streaming_reader_matches_the_v1_27_reader(tmp_path, members, cache_env, monkeypatch):
    """The buffered stream (no cache) returns exactly what the previous reader returned."""
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    previous = root / "recovery-snapshots" / "pre-v1.28.0-performance-20261115" / "backend" / "app" / "services" / "tar_cache.py"
    if not previous.is_file():
        pytest.skip("previous reader snapshot not present")
    spec = importlib.util.spec_from_file_location("tar_cache_v1_27", previous)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)

    uri = make_part(tmp_path / "part-00009.tar.zst", members)
    monkeypatch.setenv("EXTRACT_PART_CACHE", "off")
    for paths, kw in QUERIES + _verify_queries(members):
        old.clear_tar_cache()
        expected = list(old.iter_files_from_part(uri, set(paths), **kw))
        assert run(uri, paths, **kw) == expected, (paths, kw)

    wanted = {"DCIM/Camera/IMG_0001.jpg", "dup/same.txt", "empty.bin", "nope.bin"}

    def spill(module):
        out = []
        for name, temp in module.iter_spilled_files_from_part(uri, set(wanted), directory=str(tmp_path)):
            out.append((name, Path(temp).read_bytes()))
            os.unlink(temp)
        return out

    from app.services import tar_cache
    assert spill(tar_cache) == spill(old)


def test_cache_gives_space_back_to_evidence_and_drops_idle_entries(tmp_path, cache_env, monkeypatch):
    from app.services import part_cache

    monkeypatch.setenv("EXTRACT_PART_CACHE", "always")
    blobs = [(f"f{i}.bin", os.urandom(300_000)) for i in range(2)]
    uri = make_part(tmp_path / "p-space.tar.zst", blobs)
    run(uri, {"f0.bin"})
    assert part_cache.lookup(uri) is not None
    free = part_cache._disk_free(cache_env)
    # An extraction about to write more than the space above the floor evicts the cache.
    monkeypatch.setenv("EXTRACT_PART_CACHE_MIN_FREE_GB", str(max(1.0, (free - 1024) / 1024 ** 3)))
    assert part_cache.release_space(10 * 1024 ** 2) > 0
    assert part_cache.lookup(uri) is None
    monkeypatch.setenv("EXTRACT_PART_CACHE_MIN_FREE_GB", "1")
    run(uri, {"f1.bin"})
    entry = cache_env / part_cache._key(uri)
    old = time.time() - 13 * 3600
    os.utime(entry / "meta.json", (old, old))
    assert part_cache.remove_idle() == 1
    assert run(uri, {"f0.bin", "f1.bin"}) == [("f0.bin", blobs[0][1]), ("f1.bin", blobs[1][1])]


def test_extraction_releases_cache_space_before_writing_a_shard():
    text = (Path(__file__).resolve().parents[1] / "app" / "services" / "extract_shard_v45.py").read_text()
    head = text[text.index("def shard_worker_v45("):text.index("fd, tmp_name = tempfile.mkstemp(suffix=\".tar.zst\", dir=spool_dir)")]
    assert "release_space(2 * sum(" in head
