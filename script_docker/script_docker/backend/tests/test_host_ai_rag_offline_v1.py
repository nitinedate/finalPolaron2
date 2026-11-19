"""Regression tests for host Ollama, Surya OCR and non-executing evidence scanning.

These tests are deliberately offline, CPU-only and use synthetic evidence.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def offline_scanner():
    return _module("polaron_offline_malware_test", ROOT / "scripts" / "offline_malware_scan.py")


@pytest.fixture(scope="module")
def surya_adapter():
    return _module("polaron_surya_test", ROOT / "backend" / "app" / "services" / "surya_adapter.py")


def test_passive_scan_hash_ioc_and_no_evidence_writes(tmp_path, offline_scanner):
    evidence = tmp_path / "evidence"
    reports = tmp_path / "reports"
    evidence.mkdir()
    reports.mkdir()
    data = b"synthetic-unknown-binary-do-not-execute"
    (evidence / "sample.bin").write_bytes(data)
    (evidence / "noexec.exe").write_bytes(b"just bytes, not a program")
    sha = hashlib.sha256(data).hexdigest()
    indicator = reports / "analyst-indicators.txt"
    indicator.write_text(f"{sha} synthetic_test_indicator\n", encoding="utf-8")
    evidence_before = sorted((p.name, p.read_bytes()) for p in evidence.iterdir())
    result = offline_scanner.scan(evidence, reports / "before.json", indicator)
    evidence_after = sorted((p.name, p.read_bytes()) for p in evidence.iterdir())
    assert evidence_after == evidence_before
    assert result["counts"]["files_hashed"] == 2
    assert result["counts"]["ioc_hash_matches"] == 1
    assert result["counts"]["read_errors"] == 0
    assert next(item for item in result["files"] if item["path"] == "sample.bin")["classification"] == "ioc_hash_match"
    assert result["sample_execution"] is False
    assert result["destructive_operations"] is False


def test_symlink_skipped_and_nested_report_rejected(tmp_path, offline_scanner):
    evidence = tmp_path / "evidence"
    reports = tmp_path / "reports"
    evidence.mkdir()
    reports.mkdir()
    (evidence / "normal.txt").write_text("hi", encoding="utf-8")
    external = tmp_path / "outside.txt"
    external.write_text("not read", encoding="utf-8")
    try:
        (evidence / "escape.txt").symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this test platform")
    res = offline_scanner.scan(evidence, reports / "scan.json")
    assert res["counts"]["symlinks_skipped"] == 1
    assert res["counts"]["files_hashed"] == 1
    with pytest.raises(ValueError, match="nested"):
        offline_scanner.scan(evidence, evidence / "write-inside-evidence.json")


def test_before_after_does_not_claim_remediation_success(tmp_path, offline_scanner):
    evidence = tmp_path / "acquired"
    reports = tmp_path / "reports"
    evidence.mkdir()
    reports.mkdir()
    (evidence / "x.bin").write_bytes(b"before")
    offline_scanner.scan(evidence, reports / "before.json")
    # Synthetic copy: we can edit test bytes, never a real case or sample.
    (evidence / "x.bin").write_bytes(b"after")
    offline_scanner.scan(evidence, reports / "after.json")
    result = offline_scanner.compare(reports / "before.json", reports / "after.json", reports / "comparison.json")
    assert result["counts"]["content_changed"] == 1
    assert result["remediation_verified"] is False
    assert len(json.loads((reports / "comparison.json").read_text())["content_changed"]) == 1


def test_surya_v2_markup_is_preserved(surya_adapter):
    class Block:
        html = "<table><tr><td>A</td><td>B</td></tr></table>"
        confidence = 0.78

    class Result:
        blocks = [Block()]

    text, conf = surya_adapter._recognition_text(Result())
    assert "A" in text and "B" in text
    assert conf == pytest.approx(0.78)
    text, conf = surya_adapter._recognition_text({"blocks": [{"html": "<p>Case text</p>"}]})
    assert text == "Case text"
    assert conf == 0.0  # do not fabricate a confidence value


def test_surya_requires_configured_external_endpoint(monkeypatch, surya_adapter):
    monkeypatch.delenv("SURYA_INFERENCE_URL", raising=False)
    with pytest.raises(surya_adapter.SuryaUnavailable):
        surya_adapter._server_url()
    monkeypatch.setenv("SURYA_INFERENCE_URL", "http://host.docker.internal:8000/v1")
    monkeypatch.setenv("SURYA_INFERENCE_BACKEND", "vllm")
    assert surya_adapter._server_url().endswith("/v1")


def test_rag_exact_reuse_never_merges_substring(tmp_path):
    source = (ROOT / "backend" / "app" / "services" / "dual_rag_index.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_embedding_reuse_plan")
    namespace: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<rag-reuse-test>", "exec"), namespace)
    unique, positions = namespace["_embedding_reuse_plan"](["ABC", "ABC extra", "ABC"])
    assert unique == ["ABC", "ABC extra"]
    assert positions == [0, 1, 0]


def test_compose_services_have_no_docker_ollama_and_scanner_isolation():
    yaml = pytest.importorskip("yaml")
    for compose in ("docker-compose.yml", "docker-compose.https.yml"):
        d = yaml.safe_load((ROOT / compose).read_text(encoding="utf-8"))
        services = d["services"]
        assert "ollama" not in services and "ollama-init" not in services
        for svc in services.values():
            for service_id in svc.get("depends_on") or {}:
                assert service_id in services
    scanner = yaml.safe_load((ROOT / "docker-compose.malware-offline.yml").read_text())["services"]["scanner"]
    assert scanner["network_mode"] == "none"
    assert scanner["read_only"] is True
    assert scanner["cap_drop"] == ["ALL"]
    assert any(str(item).endswith(":/evidence:ro") for item in scanner["volumes"])
