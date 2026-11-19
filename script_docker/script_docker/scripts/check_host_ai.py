"""Local prerequisites for the host-Ollama + external Surya arrangement.

Can be run from a shell or inside an already-built API container.
Never pulls or executes a model and never modifies forensic evidence.
"""
from __future__ import annotations

import json
import os
import sys
from urllib.error import URLError
from urllib.request import urlopen

OLLAMA_MODELS = [
    "qllama/bge-reranker-v2-m3:latest",
    "CyberCrew/notmythos-8b:latest", "xploiter/the-xploiter:latest",
    "mistral:7b", "llama3.2:3b", "deepseek-r1:8b",
    "gemma2:27b", "qwen2.5:14b", "qwen3-coder:30b",
    "nomic-embed-text:latest", 
]


def check(base: str, path: str) -> dict:
    with urlopen(base.rstrip("/") + path, timeout=5) as resp:
        return json.load(resp)


def main():
    ollama = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    surya = os.getenv("SURYA_INFERENCE_URL", "http://localhost:8000/v1")
    bad = False
    try:
        data = check(ollama, "/api/tags")
        found = {item.get("name", "") for item in data.get("models", [])}
        print("[OK] Host Ollama:", ollama, "models:", len(found))
        for name in OLLAMA_MODELS:
            prefix = name.split(":", 1)[0]
            present = name in found or (name.endswith(":latest") and prefix in found)
            print("  [OK]" if present else "  [MISSING]", name)
    except (OSError, ValueError, URLError) as exc:
        print("[ERROR] Ollama is unreachable at", ollama, str(exc))
        bad = True
    try:
        data = check(surya, "/models")
        available = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        print("[OK] Surya vLLM:", surya, "served models:", available)
        if not available:
            bad = True
    except (OSError, ValueError, URLError) as exc:
        print("[ERROR] Surya vLLM is unreachable at", surya, str(exc))
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
