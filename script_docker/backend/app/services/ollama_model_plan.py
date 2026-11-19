"""Choose runtime roles exclusively from examiner-installed host Ollama models.

Never autoselect models absent from the documented local inventory.  The
capacity tier is only an estimate: Ollama may run models on CPU when GPU RAM
is constrained. No model is downloaded or executed by this planner.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OllamaModelPlan:
    profile: str
    vram_mb: int
    gpu_available: bool
    required: list[str]
    accuracy: list[str]
    primary: str
    review: str
    fast: str
    embed: str
    notes: list[str] = field(default_factory=list)

    @property
    def pull_models(self) -> list[str]:
        seen = set()
        ordered = []
        for name in [*self.required, *self.accuracy]:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

    def csv(self) -> str:
        return ",".join(self.pull_models)


def plan_ollama_models(*, vram_mb: int = 0, gpu_available: bool = False) -> OllamaModelPlan:
    vram = max(0, int(vram_mb or 0))
    gpu = bool(gpu_available and vram > 0)
    primary = "mistral:7b"
    review = "deepseek-r1:8b"
    fast = "llama3.2:3b"
    profile = "cpu" if not gpu else "gpu-tight"
    notes = ["Use host Ollama only; do not pull or load every installed model simultaneously"]
    if gpu and vram >= 11500:
        profile = "gpu-12"
        primary = "qwen2.5:14b"
        notes.append("14b model may spill to system memory; verify Ollama VRAM admission")
    elif gpu and vram >= 8000:
        profile = "gpu-8"
        notes.append("Prefer 7b report model on constrained GPU to reserve memory for OCR/embeddings")
    elif gpu:
        notes.append("Low VRAM; heavy models may run slowly on CPU")
    else:
        notes.append("No NVIDIA GPU; expect CPU inference delays")
    required = [fast, "mistral:7b", "deepseek-r1:8b", "nomic-embed-text:latest"]
    if primary == "qwen2.5:14b":
        required.insert(0, primary)
    return OllamaModelPlan(
        profile=profile, vram_mb=vram, gpu_available=gpu,
        required=required,
        # Large optional models are NOT in the automatic pull inventory. They
        # remain installable by the examiner via explicit model selection.
        accuracy=[],
        primary=primary, review=review, fast=fast,
        embed="nomic-embed-text:latest", notes=notes,
    )
