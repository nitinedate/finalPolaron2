"""Download and cache all three local RAG models (HF_HOME configurable).

Usage:
    python -m pip install sentence-transformers transformers torch torchvision pillow
    python download_models.py
    python download_models.py --check-only

Downloads are stored in the normal Hugging Face cache; model construction runs
on CPU so installing them never evicts evidence-processing GPU workers.
"""
from __future__ import annotations

import argparse
import gc
import importlib.util
import os
from pathlib import Path
import sys

MODELS = (
    ("BAAI/bge-m3", "Multilingual text embedding", "sentence"),
    ("sentence-transformers/clip-ViT-B-32", "Vision/text multimodal", "sentence"),
    ("cross-encoder/ms-marco-MiniLM-L-6-v2", "Lightweight reranker", "cross"),
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="Show prerequisites and model names without downloads")
    parser.add_argument("--cache-dir", type=Path, help="Bind-mounted Hugging Face cache shared with Docker workers")
    args = parser.parse_args(argv)
    if args.cache_dir:
        cache = args.cache_dir.expanduser().resolve()
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(cache)
        print("Hugging Face cache:", cache)
    print("Models to cache locally:")
    for i, (model_id, label, _) in enumerate(MODELS, 1):
        print(f" {i}. {label}: {model_id}")
    needed = [name for name in ("torch", "PIL", "sentence_transformers", "transformers")
              if importlib.util.find_spec(name) is None]
    if needed:
        print("\nMissing Python packages: " + ", ".join(needed), file=sys.stderr)
        print("Install: python -m pip install sentence-transformers transformers torch torchvision pillow", file=sys.stderr)
        return 2
    if args.check_only:
        print("\nPrerequisites detected. Run without --check-only to download models.")
        return 0
    # Imported only after verifying dependencies; avoid CUDA initialization.
    import torch
    from PIL import Image  # noqa: F401; validates user-requested Pillow dependency
    from sentence_transformers import SentenceTransformer, CrossEncoder

    print(f"\nTorch {torch.__version__}; downloading models sequentially on CPU")
    errors = []
    for index, (model_id, label, kind) in enumerate(MODELS, 1):
        print(f"\n{index}. Downloading {model_id} ({label})...", flush=True)
        try:
            if kind == "sentence":
                model = SentenceTransformer(model_id, device="cpu")
            else:
                model = CrossEncoder(model_id, device="cpu")
            del model
            gc.collect()
            print(f"[OK] {model_id}", flush=True)
        except Exception as exc:
            print(f"[FAILED] {model_id}: {exc}", file=sys.stderr, flush=True)
            errors.append(model_id)
    if errors:
        print(f"\nModel cache incomplete: {', '.join(errors)}", file=sys.stderr)
        return 1
    print("\n[SUCCESS] All three models downloaded and cached locally!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
