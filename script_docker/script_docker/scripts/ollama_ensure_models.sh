#!/bin/sh
# Legacy compatibility entry point: host Ollama only; no docker/auto-bootstrap.
# Run with OLLAMA_PULL_MISSING=1 to download missing models explicitly.
set -eu
export OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
if ! command -v ollama >/dev/null 2>&1; then
  echo 'Host Ollama CLI unavailable. Install and start Ollama on host.' >&2; exit 2
fi
models="${OLLAMA_PULL_MODELS:-mistral:7b,llama3.2:3b,deepseek-r1:8b,qwen2.5:14b,nomic-embed-text:latest}"
old_ifs=$IFS
IFS=,
for model in $models; do
  model=$(printf '%s' "$model" | tr -d ' ')
  [ -n "$model" ] || continue
  if ollama list | grep -Fq "$model"; then echo "[OK] $model"; continue; fi
  echo "[MISSING] $model"
  if [ "${OLLAMA_PULL_MISSING:-0}" = 1 ]; then ollama pull "$model"; fi
done
IFS=$old_ifs
