# Execute on Windows host (PowerShell 7 or Windows PowerShell 5.1).
# This script checks installed Ollama and Python models. It never alters evidence.
# -PullMissing explicitly opts into downloading missing Ollama models.
param([switch]$PullMissing, [switch]$LargeModels)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw 'Ollama not found on host. Install from https://ollama.com/download and start Ollama before proceeding.'
}
$required = @('qllama/bge-reranker-v2-m3:latest', 'CyberCrew/notmythos-8b:latest',
    'xploiter/the-xploiter:latest', 'mistral:7b', 'llama3.2:3b', 'deepseek-r1:8b',
    'gemma2:27b', 'qwen2.5:14b', 'qwen3-coder:30b', 'nomic-embed-text:latest')
$heavy = @('gemma2:27b', 'qwen3-coder:30b')
$installed = & ollama list 2>&1 | Out-String
Write-Host 'Host Ollama version:'
& ollama --version
foreach ($model in $required) {
    $pattern = [regex]::Escape($model.Split(':')[0])
    if ($installed -match $pattern) { Write-Host "[OK] $model"; continue }
    Write-Warning "Missing: $model"
    if ($PullMissing -and ($LargeModels -or $model -notin $heavy)) {
        & ollama pull $model
        if ($LASTEXITCODE -ne 0) { Write-Warning "Pull failed: $model. Verify the model ID in the Ollama registry." }
    }
}
# Use an isolated virtual environment. Do not change the host's global Python,
# which may belong to other examiner tools.
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvDir = Join-Path $projectRoot '.venv-host-ai'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv $venvDir
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv $venvDir
    } else {
        throw 'Python not installed. Run winget install -e --id Python.Python.3.12, then reopen PowerShell.'
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw 'Unable to create .venv-host-ai. Install Python 3.12 and retry.'
    }
}
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed in the host AI environment.' }
& $venvPython -m pip install sentence-transformers transformers torch torchvision pillow
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies could not be installed.' }
& $venvPython (Join-Path $projectRoot 'download_models.py') --cache-dir (Join-Path $projectRoot 'backup\huggingface')
if ($LASTEXITCODE -ne 0) { throw 'Hugging Face models could not be cached.' }
Write-Host 'Allow Docker containers to reach host Ollama only on a trusted bridge. Do not expose 11434 to the internet.'
Write-Host 'Start/verify a separate Surya vLLM server on port 8000 and then run scripts/check_host_ai.py.'
