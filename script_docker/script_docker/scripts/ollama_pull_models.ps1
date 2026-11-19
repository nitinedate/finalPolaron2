# Optional manual host Ollama model pulls. Does not call Docker.
param([switch]$LargeModels)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw 'Host Ollama missing. Install from ollama.com/download and start the host service.'
}
$models = @('mistral:7b', 'llama3.2:3b', 'deepseek-r1:8b', 'qwen2.5:14b',
    'nomic-embed-text:latest')
if ($env:OLLAMA_PULL_MODELS) {
    $models = @($env:OLLAMA_PULL_MODELS -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
if ($LargeModels) { $models += @('gemma2:27b', 'qwen3-coder:30b') }
$installed = & ollama list | Out-String
foreach ($model in ($models | Select-Object -Unique)) {
    if ($model -match "(?i)^(glm-ocr|zai-org/GLM-OCR)") { Write-Warning "Skipping removed OCR model $model"; continue }
    $pattern = [regex]::Escape($model.Split(':')[0])
    if ($installed -match $pattern) { Write-Host "[OK] $model"; continue }
    Write-Host "Pulling on host: $model"
    & ollama pull $model
    if ($LASTEXITCODE -ne 0) { throw "Host Ollama download failed: $model" }
}
