# Standalone Windows host Surya OCR bootstrap and server manager.
# Native llama.cpp inference; no Surya service is placed in the Polaron Compose stack.
#
# V1.31.10: Windows App Control (Smart App Control, or an organisation's App Control / WDAC policy) can
# refuse to start the downloaded llama-server.exe ("An Application Control policy has blocked this file").
# llama.cpp's Windows builds are not signed, so no other Windows build would be allowed either. When that
# happens, the same llama.cpp server runs in Docker instead (container "polaron-surya-ocr", official
# ghcr.io/ggml-org/llama.cpp image, the same cached GGUF weights, the same port 8000 and model ID); Linux
# containers are not subject to Windows App Control. The choice is remembered in surya-runtime.json, so
# later launches go straight to Docker. SURYA_HOST_RUNTIME=native|docker|auto (default auto) overrides it.
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
$runtimeStatePath = Join-Path $cache 'surya-runtime.json'
$containerName = if ($env:SURYA_DOCKER_CONTAINER) { $env:SURYA_DOCKER_CONTAINER } else { 'polaron-surya-ocr' }
$runtimeMode = if ($env:SURYA_HOST_RUNTIME) { $env:SURYA_HOST_RUNTIME.Trim().ToLowerInvariant() } else { 'auto' }
if ($runtimeMode -notin @('auto','native','docker')) { throw 'SURYA_HOST_RUNTIME must be auto, native or docker' }

function Test-AppControlBlock([string]$Text, [int]$ExitCode = 0) {
    # Start-Process: "An Application Control policy has blocked this file" (ERROR 4551 / 0x800711C7).
    # A DLL refused at load ends the process with STATUS_INVALID_IMAGE_HASH (0xC0000428 = -1073740760).
    if ($ExitCode -eq -1073740760) { return $true }
    return ([string]$Text -match '(?i)Application Control|blocked this file|0x800711C7|Smart App Control|code integrity|STATUS_INVALID_IMAGE_HASH')
}

function Get-SavedRuntime {
    try {
        if (Test-Path -LiteralPath $runtimeStatePath -PathType Leaf) { return [string]((Get-Content -LiteralPath $runtimeStatePath -Raw | ConvertFrom-Json).runtime) }
    } catch {}
    return ''
}

function Save-Runtime([string]$Runtime, [string]$Reason) {
    $body = @{ runtime = $Runtime; reason = $Reason; at_utc = (Get-Date).ToUniversalTime().ToString('o') }
    [IO.File]::WriteAllText($runtimeStatePath, ($body | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
}

function Test-ContainerRunning([string]$Name) {
    $state = (& docker inspect --format '{{.State.Running}}' $Name 2>$null | Out-String).Trim()
    return ($LASTEXITCODE -eq 0 -and $state -eq 'true')
}

function Test-DockerNvidia {
    try {
        $runtimes = (& docker info --format '{{json .Runtimes}}' 2>$null | Out-String)
        if ($LASTEXITCODE -eq 0 -and $runtimes -match '(?i)nvidia') { return $true }
    } catch {}
    # Docker Desktop (WSL 2) passes NVIDIA GPUs through when the Windows driver is installed.
    return [bool](Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)
}

function Start-SuryaDocker([string]$ModelPath, [string]$MmprojPath, [int]$Layers, [int]$Context) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker is needed to run Surya OCR here (Windows App Control blocks the native llama-server.exe), but the docker command was not found.'
    }
    & docker info --format '{{.ServerVersion}}' *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop is not running; it is needed to run Surya OCR in a container.' }
    $modelsDir = Split-Path -Parent $ModelPath
    if ((Split-Path -Parent $MmprojPath) -ne $modelsDir) { throw "Surya model files must share one folder: $ModelPath, $MmprojPath" }
    # A Polaron container that is already running (Docker restarted it) may still be loading the model.
    if (Test-ContainerRunning $containerName) {
        Write-Host ("[Surya] Waiting for the running Docker container {0} to finish loading..." -f $containerName)
        $waitUntil = (Get-Date).AddMinutes(5)
        while ((Get-Date) -lt $waitUntil -and (Test-ContainerRunning $containerName)) {
            if (Ready $endpoint) {
                try {
                    Assert-EndpointIdentity $endpoint
                    Write-Host ("[Surya] Host inference ready (Docker container {0})." -f $containerName) -ForegroundColor Green
                    return
                } catch { break }
            }
            Start-Sleep -Seconds 3
        }
    }
    $useGpu = ($env:SURYA_DOCKER_GPU -ne '0') -and (Test-DockerNvidia)
    $attempts = @()
    if ($env:SURYA_DOCKER_IMAGE) { $attempts += ,@($env:SURYA_DOCKER_IMAGE, $useGpu) }
    elseif ($useGpu) { $attempts += ,@('ghcr.io/ggml-org/llama.cpp:server-cuda', $true) }
    if (-not $env:SURYA_DOCKER_IMAGE) { $attempts += ,@('ghcr.io/ggml-org/llama.cpp:server', $false) }
    $lastError = ''
    foreach ($attempt in $attempts) {
        $image = [string]$attempt[0]
        $gpu = [bool]$attempt[1]
        # Replace only Polaron's own Surya container.
        & docker rm -f $containerName *> $null
        Write-Host ("[Surya] Running llama.cpp in Docker: {0} ({1})..." -f $image, $(if ($gpu) { 'NVIDIA GPU' } else { 'CPU' }))
        & docker pull $image
        if ($LASTEXITCODE -ne 0) { $lastError = "docker pull $image failed"; continue }
        $runArgs = @('run', '-d', '--name', $containerName, '--restart', 'unless-stopped',
            '--label', 'polaron.service=surya-ocr', '-p', ('{0}:{0}' -f $port),
            '-v', ('{0}:/models:ro' -f $modelsDir))
        if ($gpu) { $runArgs += @('--gpus', 'all') }
        $runArgs += @($image,
            '--model', ('/models/' + (Split-Path -Leaf $ModelPath)), '--mmproj', ('/models/' + (Split-Path -Leaf $MmprojPath)),
            '--host', '0.0.0.0', '--port', "$port", '--ctx-size', "$Context",
            '--n-gpu-layers', $(if ($gpu) { "$Layers" } else { '0' }), '--parallel', '1',
            '--alias', $requiredModelId, '--special')
        & docker @runArgs
        if ($LASTEXITCODE -ne 0) { $lastError = "docker run $image failed"; continue }
        $deadline = (Get-Date).AddMinutes(10)
        while ((Get-Date) -lt $deadline) {
            if (Ready $endpoint) {
                Assert-EndpointIdentity $endpoint
                Write-Host ("[Surya] Host inference ready (Docker container {0})." -f $containerName) -ForegroundColor Green
                return
            }
            if (-not (Test-ContainerRunning $containerName)) { break }
            Start-Sleep -Seconds 3
        }
        $logTail = (& docker logs --tail 25 $containerName 2>&1 | Out-String).Trim()
        $lastError = "llama.cpp in Docker ($image) did not become ready.`n$logTail"
        Write-Warning ("[Surya] " + $lastError)
    }
    throw ("Surya OCR could not be started in Docker either: " + $lastError)
}
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
        # V1.31.10: Polaron's own Docker container may be replaced (it is recreated below).
        $ownContainer = $false
        if (Get-Command docker -ErrorAction SilentlyContinue) { $ownContainer = Test-ContainerRunning $containerName }
        if ($ownContainer) {
            & docker rm -f $containerName *> $null
        } else {
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
        }
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
$useDocker = ($runtimeMode -eq 'docker') -or ($runtimeMode -eq 'auto' -and (Get-SavedRuntime) -eq 'docker')
if ($useDocker) { Write-Host '[Surya] Runtime: Docker (Windows App Control blocked the native server earlier, or SURYA_HOST_RUNTIME=docker).' }
$resultFile = Join-Path $cache ("surya-bootstrap-$([guid]::NewGuid().ToString('N')).json")
try {
    $bootstrapArgs = @((Join-Path $root 'scripts\surya_host_bootstrap.py'), '--cache', $cache, '--backend', $backend, '--result-file', $resultFile)
    if ($useDocker) { $bootstrapArgs += '--model-only' }
    & $python @bootstrapArgs
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
    if (-not $useDocker -and [string]::IsNullOrWhiteSpace($server)) { throw 'Surya installer did not select a llama-server.exe.' }
} finally {
    Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue
}
foreach ($file in @($model,$mmproj)) { if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Surya provisioned file missing: $file" } }
# Docker Desktop's host.docker.internal requires the service to listen on the
# host interface, not just loopback. Do not expose this port through a router.
$gpuLayers = if ($env:SURYA_HOST_GPU_LAYERS) { [int]$env:SURYA_HOST_GPU_LAYERS } elseif ($backend -eq 'cpu') { 0 } else { 12 }
$ctx = if ($env:SURYA_HOST_CONTEXT) { [int]$env:SURYA_HOST_CONTEXT } else { 24576 }
if ($useDocker) {
    Start-SuryaDocker -ModelPath $model -MmprojPath $mmproj -Layers $gpuLayers -Context $ctx
    exit 0
}
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { throw "Surya provisioned file missing: $server" }
Write-Host "[Surya] Selected llama.cpp executable: $server"
$argList = @('--model', ('"' + $model + '"'), '--mmproj', ('"' + $mmproj + '"'), '--host', '0.0.0.0', '--port', "$port", '--ctx-size', "$ctx", '--n-gpu-layers', "$gpuLayers", '--parallel', '1', '--alias', $requiredModelId, '--special')
Write-Host "[Surya] Starting Windows host llama.cpp server (GPU layers=$gpuLayers; context=$ctx)..."
$blocked = ''
$proc = $null
try {
    $proc = Start-Process -FilePath $server -ArgumentList $argList -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
} catch {
    if (-not (Test-AppControlBlock $_.Exception.Message)) { throw }
    $blocked = [string]$_.Exception.Message
}
if ($proc) {
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
    $exitCode = 0
    if ($proc.HasExited) { try { $exitCode = [int]$proc.ExitCode } catch { $exitCode = 0 } }
    if (-not (Test-AppControlBlock $details $exitCode)) {
        throw "Surya inference failed to reach ready state. Log: $stderrPath`n$details"
    }
    $blocked = "llama-server.exe was stopped by Windows (exit $exitCode). $details"
}
# Windows App Control refused the native server.
Write-Warning ("[Surya] Windows App Control blocked the native llama.cpp server: " + $blocked.Trim())
if ($runtimeMode -eq 'native') {
    throw ("Windows App Control blocked $server and SURYA_HOST_RUNTIME=native. Allow it in your App Control policy, " +
        "or remove SURYA_HOST_RUNTIME (or set it to docker) so Surya runs in Docker.")
}
Write-Host '[Surya] Switching to the same llama.cpp server in Docker (not subject to Windows App Control); later launches use Docker directly.' -ForegroundColor Yellow
Save-Runtime -Runtime 'docker' -Reason ('app_control: ' + $blocked.Trim())
Start-SuryaDocker -ModelPath $model -MmprojPath $mmproj -Layers $gpuLayers -Context $ctx
exit 0
