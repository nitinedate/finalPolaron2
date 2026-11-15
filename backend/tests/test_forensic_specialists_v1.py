"""Offline regression tests: strict evidence, task routing, safe failures and on-demand semantics."""
import json

from app.services import forensic_specialists as sp


class FakeSettings:
    forensic_specialist_enabled = True
    vulnerability_specialist_enabled = True
    forensic_security_model = "CyberCrew/notmythos-8b:latest"
    vulnerability_reasoning_model = "xploiter/the-xploiter:latest"
    ai_specialist_timeout_sec = 15
    ai_specialist_max_concurrent = 1
    ai_specialist_max_evidence_chars = 6000


def _ready(monkeypatch, output):
    monkeypatch.setattr(sp, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(sp.model_router, "list_models", lambda: [FakeSettings.forensic_security_model, FakeSettings.vulnerability_reasoning_model])
    monkeypatch.setattr(sp.model_router, "_ollama_post", lambda *args, **kwargs: {"message": {"content": json.dumps(output)}})


def test_task_routes_to_specialists_and_unknown_disabled():
    settings = FakeSettings()
    assert sp.select_specialist("incident_analysis", settings) == settings.forensic_security_model
    assert sp.select_specialist("disk_security", settings) == settings.forensic_security_model
    assert sp.select_specialist("mobile_security", settings) == settings.forensic_security_model
    assert sp.select_specialist("malware_behavior", settings) == settings.forensic_security_model
    assert sp.select_specialist("vulnerability_exploitability", settings) == settings.vulnerability_reasoning_model
    assert sp.select_specialist("attack_path_review", settings) == settings.vulnerability_reasoning_model
    assert sp.select_specialist("forensic_extraction", settings) is None
    settings.vulnerability_specialist_enabled = False
    assert sp.select_specialist("attack_path_review", settings) is None


def test_prepare_sources_has_strict_budget_and_paths():
    src = sp.prepare_sources([{"id": 7, "content": "abc" * 1000, "file_path": "disk/image1"}], total_chars=130)
    assert len(src["E1"]["content"]) == 130
    assert src["E1"]["file_path"] == "disk/image1"


def test_unverified_hallucinated_quotes_rejected():
    src = {"E1": {"content": "PowerShell command launched from a user profile at 10:00", "id": "123", "file_path": "log"}}
    payload = {"findings": [{"source_id": "E1", "quote": "fabricated marker does not exist", "observation": "infected"}]}
    findings, rejected = sp.validate_findings(payload, src)
    assert not findings and rejected == 1


def test_validated_candidates_never_claim_final_verification():
    src = {"E1": {"content": "PowerShell command launched from a user profile at 10:00", "id": "123", "file_path": "log"}}
    payload = {"findings": [{"source_id": "E1", "quote": "PowerShell command launched", "observation": "Possible activity", "assessment": "supported"}]}
    findings, rejected = sp.validate_findings(payload, src)
    assert len(findings) == 1 and rejected == 0
    assert findings[0]["status"] == "candidate_examiners_review_required"
    assert findings[0]["chunk_id"] == "123"


def test_disabled_and_missing_model_are_not_silent(monkeypatch):
    settings = FakeSettings()
    settings.forensic_specialist_enabled = False
    monkeypatch.setattr(sp, "get_settings", lambda: settings)
    r = sp.analyze(job_id="job", task_type="incident_analysis", question="Some question", evidence_rows=[])
    assert r["status"] == "unavailable"
    settings.forensic_specialist_enabled = True
    monkeypatch.setattr(sp.model_router, "list_models", lambda: [])
    r = sp.analyze(job_id="job", task_type="incident_analysis", question="Some question", evidence_rows=[{"content": "text sample of evidence"}])
    assert r["status"] == "unavailable"
    assert not r["actions_performed"]


def test_cited_json_is_returned_and_unverified_rejected(monkeypatch):
    _ready(monkeypatch, {"findings": [
        {"source_id": "E1", "quote": "suspicious scheduled task discovered", "observation": "Inspect task", "assessment": "possible"},
        {"source_id": "E2", "quote": "invented", "observation": "Fake"},
    ]})
    r = sp.analyze(job_id="j1", task_type="incident_analysis", question="What happened", evidence_rows=[{"id": "1", "file_path": "a", "content": "suspicious scheduled task discovered in registry"}])
    assert r["status"] == "review_required"
    assert len(r["findings"]) == 1
    assert r["rejected_findings"] == 1
    assert r["remediation_verified"] is False
    assert r["actions_performed"] is False


def test_model_errors_are_reported_as_errors(monkeypatch):
    _ready(monkeypatch, {})
    def boom(*args, **kwargs):
        raise TimeoutError("test")
    monkeypatch.setattr(sp.model_router, "_ollama_post", boom)
    r = sp.analyze(job_id="j1", task_type="attack_path_review", question="impact?", evidence_rows=[{"content": "Scanner identified a vulnerable service"}])
    assert r["status"] == "model_error" and not r["findings"]


def test_no_evidence_refuses_inference(monkeypatch):
    _ready(monkeypatch, {})
    r = sp.analyze(job_id="j1", task_type="malware_behavior", question="behavior?", evidence_rows=[])
    assert r["status"] == "insufficient_evidence"


def test_invalid_task_and_unscoped_are_blocked(monkeypatch):
    _ready(monkeypatch, {})
    import pytest
    with pytest.raises(ValueError):
        sp.analyze(job_id="j1", task_type="unknown", question="x", evidence_rows=[])
    with pytest.raises(ValueError):
        sp.analyze(job_id="j1", task_type="incident_analysis", question="x", evidence_rows=[], is_scoped=False)
