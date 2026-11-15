#!/bin/sh
# Optional manual download of explicitly selected models into host-installed Ollama.
# Nothing in Docker Compose starts or owns an Ollama server.
set -eu
export OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
if ! command -v ollama >/dev/null 2>&1; then
  echo 'Host Ollama CLI not available' >&2; exit 2
fi
MODELS="${OLLAMA_PULL_MODELS:-mistral:7b,llama3.2:3b,deepseek-r1:8b,qwen2.5:14b,nomic-embed-text:latest}"
printf 'Using host Ollama: %s\n' "$OLLAMA_HOST"
old_ifs=$IFS
IFS=,
for model in $MODELS; do
  model=$(printf '%s' "$model" | tr -d ' ')
  [ -n "$model" ] || continue
  case "$model" in glm-ocr*|zai-org/GLM-OCR*) echo "Skipping removed legacy OCR model"; continue ;; esac
  echo "Checking $model"
  if ollama list | grep -Fq "$model"; then echo "  already installed"; continue; fi
  echo "  pulling $model"
  ollama pull "$model"
done
IFS=$old_ifs
