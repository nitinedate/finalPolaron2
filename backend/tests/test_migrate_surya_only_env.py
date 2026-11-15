"""Migration must preserve secrets and unrelated evidence settings."""
from pathlib import Path
import importlib.util

def _module():
    path=Path(__file__).resolve().parents[2]/"scripts/migrate_surya_only_env.py"
    spec=importlib.util.spec_from_file_location("migrate_surya_env",path)
    m=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_legacy_config_preserves_secrets():
    m=_module()
    original="POSTGRES_PASSWORD=SECRET\nOCR_ENGINE=glm-ocr\nOCR_MODEL=zai-org/GLM-OCR\nOLLAMA_PULL_MODELS=qwen2.5:14b,glm-ocr:latest\n"
    out,changes=m.migrated(original)
    assert "POSTGRES_PASSWORD=SECRET" in out
    assert "OCR_ENGINE=surya" in out
    assert "OCR_MODEL=datalab-to/surya-ocr-2.5" in out
    assert "OLLAMA_PULL_MODELS=qwen2.5:14b" in out
    assert len(changes)>=3

def test_already_migrated_does_not_change():
    m=_module()
    out,notes=m.migrated("OCR_ENGINE=surya\nOCR_MODEL=datalab-to/surya-ocr-2.5\n")
    assert notes==[]
