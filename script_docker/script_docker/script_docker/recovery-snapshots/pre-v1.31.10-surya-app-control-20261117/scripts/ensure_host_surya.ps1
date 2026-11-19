# Standalone Windows host Surya OCR bootstrap and server manager.
# Native llama.cpp inference; no Surya service is placed in the Polaron Compose stack.
[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [switch]$ValidateOnly,
    [switch]$SkipInstall
)
$ErrorActionPreference = 'Stop'
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$cache = if ($env:SURYA_HOST_CACHE_DIR) { $env:SURYA_HOST_CACHE_DIR } else { Join-Path $env:LOCALAPPDATA 'Polaron\SuryaOCR' }
$port = if ($env:SURYA_HOST_PORT) { [int]$env:SURYA_HOST_PORT } else { 8000 }
$endpoint = "http://127.0.0.1:$port/v1"
$statePath = Join-Path $cache 'surya-server.pid'
$stdoutPath = Join-Path $cache 'surya-server.out.log'
$stderrPath = Join-Path $cache 'surya-server.err.log'
$venv = Join-Path $cache 'python'
$python = Join-Path $venv 'Scripts\python.exe'
$requiredModelId = 'datalab-to/surya-ocr-2.5'
$backend = if ($env:SURYA_HOST_ACCELERATOR) { $env:SURYA_HOST_ACCELERATOR } else { 'vulkan' }
if ($backend -notin @('vulkan','cpu')) { throw 'SURYA_HOST_ACCELERATOR must be vulkan or cpu' }
function Ready([string]$url) {
    try {
        $response = Invoke-RestMethod -Uri "$url/models" -TimeoutSec 4 -ErrorAction Stop
        return ($null -ne $response.data -and @($response.data).Count -gt 0)
    } catch { return $false }
}
function Assert-EndpointIdentity([string]$url) {
    # Surya's llama.cpp client compares the EXACT advertised model ID.
    # A GGUF filesystem path is not equivalent to the expected ID.
    $reply = Invoke-RestMethod -Uri "$url/models" -TimeoutSec 5 -ErrorAction Stop
    $models = @($reply.data | ForEach-Object { [string]$_.id })
    if ($models -notcontains $requiredModelId) {
        throw "Surya model mismatch on $url. Expected '$requiredModelId'; advertised '$($models -join ', ')'. Restart ONLY the Polaron-owned Surya host llama-server with --alias $requiredModelId; do not terminate an unknown server."
    }
}
Write-Host "[Surya] Endpoint: $endpoint; cache: $cache" -ForegroundColor Cyan
if (Ready $endpoint) {
    try {
        Assert-EndpointIdentity $endpoint
        Write-Host '[Surya] Existing host OCR server is ready.' -ForegroundColor Green
        exit 0
    } catch {
        if ($ValidateOnly -or $SkipInstall) { throw }
        # Only the process started by this launcher may be replaced. Never
        # terminate an arbitrary program occupying this port.
        if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) { throw }
        $savedPid = 0
        if (-not [int]::TryParse((Get-Content -LiteralPath $statePath -Raw).Trim(), [ref]$savedPid)) { throw }
        $owned = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction Stop
        if ($null -eq $owned -or $owned.Name -ne 'llama-server.exe' -or
            $owned.CommandLine -notmatch 'surya-2\.5\.gguf' -or
            $owned.CommandLine -notmatch '--mmproj' -or
            $owned.CommandLine -notmatch ('--port\s+' + [regex]::Escape([string]$port) + '(\s|$)')) { throw }
        Write-Warning '[Surya] Polaron-owned host OCR server advertises the wrong model ID. Restarting ONLY the verified managed process with the correct --alias.'
        Stop-Process -Id $savedPid -ErrorAction Stop
        $deadlineStop = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadlineStop -and (Ready $endpoint)) { Start-Sleep -Milliseconds 500 }
        if (Ready $endpoint) { throw 'Surya endpoint remains occupied after stopping managed server; refusing to start another writer.' }
    }
}
if ($ValidateOnly) {
    Write-Warning "Surya host OCR is not running at $endpoint. No installation performed in ValidateOnly."
    exit 0
}
if ($SkipInstall) { throw "Surya is not ready at $endpoint and SkipInstall was requested." }
New-Item -ItemType Directory -Path $cache -Force | Out-Null
if (-not (Test-Path -LiteralPath $python)) {
    $base = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($base) { & py -3.12 -m venv $venv; if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required. Install Python 3.12 and enable py launcher.' } }
    else {
        $base = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $base) { throw 'Python 3.12 not found. Install Python 3.12, then rerun the launcher.' }
        & python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create Surya host virtual environment.' }
    }
}
# Host Surya installation and Hugging Face model cache are persistent.
$marker = Join-Path $cache '.surya-host-packages-v2'
if (-not (Test-Path -LiteralPath $marker)) {
    Write-Host '[Surya] Installing Surya OCR client and Hugging Face downloader in isolated host environment...'
    & $python -m pip install --disable-pip-version-check 'surya-ocr>=0.23.0,<0.25.0' 'huggingface_hub>=0.26,<2'
    if ($LASTEXITCODE -ne 0) { throw 'Surya host Python install failed. Review network access, Python version, and pip logs.' }
    New-Item -ItemType File -Path $marker -Force | Out-Null
}
Write-Host '[Surya] Checking model weights and llama.cpp server binaries...'
# The installer now uses an immutable runtime directory, not a Windows-locked
# rename into the legacy llama.cpp directory. Read its actual binary path.
$resultFile = Join-Path $cache ("surya-bootstrap-$([guid]::NewGuid().ToString('N')).json")
try {
    & $python (Join-Path $root 'scripts\surya_host_bootstrap.py') --cache $cache --backend $backend --result-file $resultFile
    if ($LASTEXITCODE -ne 0) {
        throw 'Surya llama.cpp provisioning failed. Read the [Surya ERROR] above. Both GGUF model weights remain cached.'
    }
    if (-not (Test-Path -LiteralPath $resultFile -PathType Leaf)) {
        throw 'Surya installer returned success without a machine-readable server path.'
    }
    $resolved = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
    $model = [string]$resolved.model
    $mmproj = [string]$resolved.mmproj
    $server = [string]$resolved.server
    if ([string]::IsNullOrWhiteSpace($server)) { throw 'Surya installer did not select a llama-server.exe.' }
} finally {
    Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue
}
Write-Host "[Surya] Selected llama.cpp executable: $server"
foreach ($file in @($model,$mmproj,$server)) { if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Surya provisioned file missing: $file" } }
# Docker Desktop's host.docker.internal requires the service to listen on the
# host interface, not just loopback. Do not expose this port through a router.
$gpuLayers = if ($env:SURYA_HOST_GPU_LAYERS) { [int]$env:SURYA_HOST_GPU_LAYERS } elseif ($backend -eq 'cpu') { 0 } else { 12 }
$ctx = if ($env:SURYA_HOST_CONTEXT) { [int]$env:SURYA_HOST_CONTEXT } else { 24576 }
$argList = @('--model', ('"' + $model + '"'), '--mmproj', ('"' + $mmproj + '"'), '--host', '0.0.0.0', '--port', "$port", '--ctx-size', "$ctx", '--n-gpu-layers', "$gpuLayers", '--parallel', '1', '--alias', $requiredModelId, '--special')
Write-Host "[Surya] Starting Windows host llama.cpp server (GPU layers=$gpuLayers; context=$ctx)..."
$proc = Start-Process -FilePath $server -ArgumentList $argList -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
Set-Content -LiteralPath $statePath -Value $proc.Id
$deadline = (Get-Date).AddMinutes(7)
while ((Get-Date) -lt $deadline) {
    if (Ready $endpoint) {
        Assert-EndpointIdentity $endpoint
        Write-Host '[Surya] Host inference ready.' -ForegroundColor Green
        exit 0
    }
    if ($proc.HasExited) { break }
    Start-Sleep -Seconds 3
    $proc.Refresh()
}
$details = if (Test-Path $stderrPath) { @(Get-Content $stderrPath -Tail 20) -join "`n" } else { 'No stderr log' }
throw "Surya inference failed to reach ready state. Log: $stderrPath`n$details"
