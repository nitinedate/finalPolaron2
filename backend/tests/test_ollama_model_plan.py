"""Host Ollama planning tests for the explicitly supplied model inventory."""

from app.services.ollama_model_plan import plan_ollama_models

KNOWN = {
    "qllama/bge-reranker-v2-m3:latest", "CyberCrew/notmythos-8b:latest",
    "xploiter/the-xploiter:latest", "mistral:7b", "llama3.2:3b",
    "deepseek-r1:8b", "gemma2:27b", "qwen2.5:14b",
    "qwen3-coder:30b", "nomic-embed-text:latest",
}


def test_cpu_uses_small_declared_models():
    plan = plan_ollama_models(vram_mb=0, gpu_available=False)
    assert plan.primary == "mistral:7b"
    assert plan.fast == "llama3.2:3b"
    assert plan.embed == "nomic-embed-text:latest"
    assert set(plan.pull_models) <= KNOWN
    assert not plan.accuracy


def test_laptop_12gb_uses_declared_14b_model():
    plan = plan_ollama_models(vram_mb=12288, gpu_available=True)
    assert plan.primary == "qwen2.5:14b"
    assert "qwen2.5:14b" in plan.required
    assert plan.review == "deepseek-r1:8b"
    assert set(plan.pull_models) <= KNOWN
    assert "qwen3-coder:30b" not in plan.pull_models


def test_tight_gpu_uses_small_declared_models():
    plan = plan_ollama_models(vram_mb=8192, gpu_available=True)
    assert plan.primary == "mistral:7b"
    assert "qwen2.5:14b" not in plan.pull_models
    assert set(plan.pull_models) <= KNOWN


def test_high_memory_never_autopulls_unspecified_models():
    for vram in (24576, 40960, 65536):
        plan = plan_ollama_models(vram_mb=vram, gpu_available=True)
        assert plan.primary == "qwen2.5:14b"
        assert set(plan.pull_models) <= KNOWN
        assert "gemma2:27b" not in plan.pull_models
        assert "qwen3-coder:30b" not in plan.pull_models
