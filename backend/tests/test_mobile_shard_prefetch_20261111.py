"""Hermetic regression checks: no database, MinIO or evidence required."""
import importlib.util
import json
import sys
import types
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "app/services/mobile_forensic/shard_prefetch.py"
spec = importlib.util.spec_from_file_location("mobile_shard_prefetch_under_test", MODULE)
prefetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prefetch)


def item(path, part, size, **meta):
    return {"path": path, "size": size, "extension": ".db", "meta": {"extracted_part_uri": part, **meta}, "sha256": None}


def test_group_only_multiple_small_same_part():
    claim = [{"payload": [item("a", "part-1", 20), item("b", "part-1", 30), item("c", "part-2", 5)]}]
    assert prefetch.choose_prefetch_groups(claim, max_member=100, max_batch=100) == {"part-1": [("a", 20, None), ("b", 30, None)]}


def test_bounds_duplicate_and_derivation():
    claim = [{"payload": [item("a", "one", 20), item("a", "one", 20), item("b", "one", 500), item("c", "one", 30, whatsapp_derivation=True), item("d", "one", 30)]}]
    assert list(prefetch.choose_prefetch_groups(claim, max_member=100, max_batch=60)) == ["one"]
    assert [x[0] for x in prefetch.choose_prefetch_groups(claim, max_member=100, max_batch=60)["one"]] == ["a", "d"]


def test_string_payload_and_member_filter():
    rows = [item("a", "one", 10), item("b", "one", 10)]
    assert prefetch.choose_prefetch_groups([{"payload": json.dumps(rows)}], wanted={"a"}) == {}


def test_prefetch_verification_and_fallback(monkeypatch):
    calls = []
    fake_models = types.ModuleType("app.services.mobile_forensic.models")
    class InventoryItem:
        def __init__(self, **kw):
            self.path = kw["path"]
    fake_models.InventoryItem = InventoryItem
    fake_tar = types.ModuleType("app.services.tar_cache")
    def stream(uri, paths, **kwargs):
        calls.append((uri, paths, kwargs))
        yield "a", b"ABCD"
        yield "b", None  # integrity failure => keep normal source read fallback
    fake_tar.iter_files_from_part = stream
    monkeypatch.setitem(sys.modules, "app", types.ModuleType("app"))
    monkeypatch.setitem(sys.modules, "app.services", types.ModuleType("app.services"))
    monkeypatch.setitem(sys.modules, "app.services.mobile_forensic", types.ModuleType("app.services.mobile_forensic"))
    monkeypatch.setitem(sys.modules, "app.services.mobile_forensic.models", fake_models)
    monkeypatch.setitem(sys.modules, "app.services.tar_cache", fake_tar)
    registry = types.SimpleNamespace(route=lambda item, ctx: [types.SimpleNamespace(name="native_sqlite")])
    ctx = types.SimpleNamespace(extra={"prefetched_bytes": {}})
    rows = [item("a", "one", 4), item("b", "one", 4)]
    assert prefetch.prefetch_claim_bytes([{"payload": rows}], registry, ctx) == 4
    assert ctx.extra["prefetched_bytes"] == {"a": (b"ABCD", 4)}
    assert len(calls) == 1
    assert calls[0][2]["verify_size_by_path"] == {"a": 4, "b": 4}


def test_prefetch_opt_out(monkeypatch):
    monkeypatch.setenv("MOBILE_SHARD_PREFETCH", "false")
    assert prefetch.prefetch_claim_bytes([], None, None) == 0


def test_dense_shard_wins_even_when_metadata_for_scattered_files_comes_first():
    # Metadata processing must NOT consume the byte budget before shard ranking.
    scattered = [item(f"x{i}", f"scattered{i}", 10) for i in range(30)]
    dense = [item(f"dense{i}", "preferred", 10) for i in range(6)]
    groups = prefetch.choose_prefetch_groups(
        [{"payload": scattered + dense}], max_member=100, max_batch=60
    )
    assert list(groups) == ["preferred"]
    assert len(groups["preferred"]) == 6


def test_byte_budget_and_multiple_shards_always_respected():
    rows = [item(f"a{i}", "pA", 10) for i in range(6)]
    rows += [item(f"b{i}", "pB", 10) for i in range(5)]
    rows += [item(f"c{i}", "pC", 10) for i in range(4)]
    groups = prefetch.choose_prefetch_groups([{"payload": rows}], max_batch=90)
    assert list(groups) == ["pA", "pB"]
    assert sum(size for entries in groups.values() for _, size, _ in entries) <= 90


def test_header_budget_does_not_rescan_large_cache_files():
    rows = [item(f"cache{i}.exo", "part", 5_000_000) for i in range(20)]
    claim = [{"payload": rows}]
    full = prefetch.choose_prefetch_groups(claim, max_member=8_000_000, max_batch=12_000_000)
    capped = prefetch.choose_prefetch_groups(
        claim, max_member=8_000_000, max_batch=12_000_000,
        retain_bytes={row["path"]: 4096 for row in rows},
    )
    assert len(full["part"]) == 2
    assert len(capped["part"]) == 20
    assert capped["part"][0][1] == 5_000_000


def test_bad_size_is_not_fatal_to_other_forensic_files():
    rows = [item("broken", "p", "corrupt"), item("a", "p", 5), item("b", "p", 5)]
    groups = prefetch.choose_prefetch_groups([{"payload": rows}], max_batch=10)
    assert list(groups) == ["p"]
    assert len(groups["p"]) == 2
