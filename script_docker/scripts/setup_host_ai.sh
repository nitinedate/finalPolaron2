#!/usr/bin/env bash
# Run on trusted Linux host. Does not run Docker or modify evidence.
set -euo pipefail
if ! command -v ollama >/dev/null; then
  echo 'Install host Ollama from https://ollama.com/download first.' >&2; exit 2
fi
pull_missing="${OLLAMA_PULL_MISSING:-0}"
models=(qllama/bge-reranker-v2-m3:latest CyberCrew/notmythos-8b:latest
  xploiter/the-xploiter:latest mistral:7b llama3.2:3b deepseek-r1:8b
  gemma2:27b qwen2.5:14b qwen3-coder:30b nomic-embed-text:latest)
for model in "${models[@]}"; do
  if ollama list | grep -Fq "${model%%:*}"; then echo "[OK] $model"; continue; fi
  echo "[MISSING] $model"
  if [[ "$pull_missing" = 1 ]]; then ollama pull "$model"; fi
done
project_root="$(cd "$(dirname "$0")/.." && pwd)"
venv_python="$project_root/.venv-host-ai/bin/python"
if [[ ! -x "$venv_python" ]]; then
  python3 -m venv "$project_root/.venv-host-ai"
fi
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install sentence-transformers transformers torch torchvision pillow
"$venv_python" "$project_root/download_models.py" --cache-dir "$project_root/backup/huggingface"
echo 'Ensure OLLAMA_HOST binds only to the trusted Docker bridge; firewall port 11434 against external access.'
echo 'Start separate Surya vLLM (model checkpoint datalab-to/surya-ocr-2.5) on host port 8000.'
