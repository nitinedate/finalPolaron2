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
#
# V1.31.11: Smart App Control can also let llama-server.exe start and then block one of its DLLs ("Smart App
# Control has blocked part of this app"); the server then simply never becomes ready. So in auto mode:
#   - when Smart App Control is on, or Windows enforces code integrity for applications, Docker is used
#     straight away (the unsigned Windows build cannot run there, and each try shows a Windows warning);
#   - otherwise the Windows build is tried, and if it does not become ready for any reason, Docker is used
#     and remembered (delete surya-runtime.json to try the Windows build again).
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

function Get-AppControlState {
    # Smart App Control: HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy VerifiedAndReputablePolicyState
    # (0 off, 1 on, 2 evaluation). App Control for Business / WDAC: Win32_DeviceGuard user-mode code
    # integrity (2 = enforced). Both block unsigned programs and DLLs such as llama.cpp's Windows build.
    try {
        $sac = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -ErrorAction Stop
        if ($sac.PSObject.Properties['VerifiedAndReputablePolicyState'] -and [int]$sac.VerifiedAndReputablePolicyState -eq 1) { return 'Smart App Control is on' }
    } catch {}
    try {
        $dg = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard -OperationTimeoutSec 10 -ErrorAction Stop
        if ($dg -and [int]$dg.UsermodeCodeIntegrityPolicyEnforcementStatus -eq 2) { return 'Windows App Control enforces code integrity for applications' }
    } catch {}
    return ''
}

function Get-CodeIntegrityBlocks([datetime]$Since, [string]$Folder) {
    # Windows records each blocked file in the Code Integrity log (events 3033 / 3077).
    try {
        $events = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; Id = 3033, 3077; StartTime = $Since } -MaxEvents 20 -ErrorAction Stop)
        $hits = @($events | Where-Object { [string]$_.Message -match [regex]::Escape($Folder) -or [string]$_.Message -match '(?i)llama|ggml' })
        if ($hits.Count) { return ($hits | Select-Object -First 3 | ForEach-Object { ([string]$_.Message -split "`n")[0].Trim() }) -join ' | ' }
    } catch {}
    return ''
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

function Invoke-Docker {
    # V1.31.12: every docker call goes through here. In Windows PowerShell 5.1 a native command's stderr
    # (for example "Error: no such object: polaron-surya-ocr" while checking whether the container exists)
    # becomes a terminating error under ErrorActionPreference=Stop, which stopped the Docker fallback at
    # its first check. Here stderr is ordinary text and only the exit code decides.
    param([string[]]$DockerArgs, [switch]$Show)
    $ErrorActionPreference = 'Continue'
    $lines = New-Object System.Collections.Generic.List[string]
    & docker @DockerArgs 2>&1 | ForEach-Object {
        $line = [string]$_
        $lines.Add($line)
        if ($Show) { Write-Host $line }
    }
    $code = $LASTEXITCODE
    return [pscustomobject]@{ Code = [int]$code; Text = (($lines.ToArray()) -join "`n").Trim() }
}

function Test-ContainerRunning([string]$Name) {
    $r = Invoke-Docker @('inspect', '--format', '{{.State.Running}}', $Name)
    return ($r.Code -eq 0 -and $r.Text -eq 'true')
}

function Test-DockerNvidia {
    try {
        $runtimes = Invoke-Docker @('info', '--format', '{{json .Runtimes}}')
        if ($runtimes.Code -eq 0 -and $runtimes.Text -match '(?i)nvidia') { return $true }
    } catch {}
    # Docker Desktop (WSL 2) passes NVIDIA GPUs through when the Windows driver is installed.
    return [bool](Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)
}

function Start-SuryaDocker([string]$ModelPath, [string]$MmprojPath, [int]$Layers, [int]$Context) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker is needed to run Surya OCR here (Windows App Control blocks the native llama-server.exe), but the docker command was not found.'
    }
    $info = Invoke-Docker @('info', '--format', '{{.ServerVersion}}')
    if ($info.Code -ne 0) { throw ('Docker Desktop is not running; it is needed to run Surya OCR in a container. ' + $info.Text) }
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
        $null = Invoke-Docker @('rm', '-f', $containerName)
        Write-Host ("[Surya] Running llama.cpp in Docker: {0} ({1})..." -f $image, $(if ($gpu) { 'NVIDIA GPU' } else { 'CPU' }))
        $pull = Invoke-Docker @('pull', $image) -Show
        if ($pull.Code -ne 0) { $lastError = "docker pull $image failed: $($pull.Text)"; Write-Warning ("[Surya] " + $lastError); continue }
        $runArgs = @('run', '-d', '--name', $containerName, '--restart', 'unless-stopped',
            '--label', 'polaron.service=surya-ocr', '-p', ('{0}:{0}' -f $port),
            '-v', ('{0}:/models:ro' -f $modelsDir))
        if ($gpu) { $runArgs += @('--gpus', 'all') }
        $runArgs += @($image,
            '--model', ('/models/' + (Split-Path -Leaf $ModelPath)), '--mmproj', ('/models/' + (Split-Path -Leaf $MmprojPath)),
            '--host', '0.0.0.0', '--port', "$port", '--ctx-size', "$Context",
            '--n-gpu-layers', $(if ($gpu) { "$Layers" } else { '0' }), '--parallel', '1',
            '--alias', $requiredModelId, '--special')
        $started = Invoke-Docker $runArgs
        if ($started.Code -ne 0) { $lastError = "docker run $image failed: $($started.Text)"; Write-Warning ("[Surya] " + $lastError); continue }
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
        $logTail = (Invoke-Docker @('logs', '--tail', '25', $containerName)).Text
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
            $null = Invoke-Docker @('rm', '-f', $containerName)
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
if ($useDocker) { Write-Host '[Surya] Runtime: Docker (the Windows build could not run here earlier, or SURYA_HOST_RUNTIME=docker).' }
elseif ($runtimeMode -eq 'auto') {
    $appControl = Get-AppControlState
    if ($appControl) {
        Write-Host ("[Surya] {0}: llama.cpp's unsigned Windows build cannot run on this computer. Using the same server in Docker." -f $appControl) -ForegroundColor Yellow
        Save-Runtime -Runtime 'docker' -Reason $appControl
        $useDocker = $true
    }
}
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
$failure = ''
$proc = $null
$startedAt = Get-Date
try {
    $proc = Start-Process -FilePath $server -ArgumentList $argList -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
} catch {
    $failure = [string]$_.Exception.Message
    if (Test-AppControlBlock $failure) { $blocked = $failure }
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
    else {
        # Not ready in time: stop only the process this script started, so Docker can use the port.
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    $failure = "Surya inference failed to reach ready state (exit $exitCode). Log: $stderrPath`n$details"
    $ci = Get-CodeIntegrityBlocks -Since $startedAt -Folder (Split-Path -Parent $server)
    if ($ci) { $blocked = "Windows blocked a llama.cpp file: $ci" }
    elseif (Test-AppControlBlock $details $exitCode) { $blocked = "llama-server.exe was stopped by Windows (exit $exitCode). $details" }
}
$reason = $(if ($blocked) { 'app_control: ' + $blocked.Trim() } else { 'native_failed: ' + $failure.Trim() })
if ($blocked) { Write-Warning ("[Surya] Windows App Control blocked the native llama.cpp server: " + $blocked.Trim()) }
else { Write-Warning ("[Surya] The Windows llama.cpp server did not start: " + $failure.Trim()) }
if ($runtimeMode -eq 'native') {
    if ($blocked) {
        throw ("Windows App Control blocked $server and SURYA_HOST_RUNTIME=native. Allow it in your App Control policy, " +
            "or remove SURYA_HOST_RUNTIME (or set it to docker) so Surya runs in Docker.")
    }
    throw $failure
}
Write-Host '[Surya] Switching to the same llama.cpp server in Docker; later launches use Docker directly (delete surya-runtime.json to try the Windows build again).' -ForegroundColor Yellow
try {
    Start-SuryaDocker -ModelPath $model -MmprojPath $mmproj -Layers $gpuLayers -Context $ctx
} catch {
    throw ("The Windows llama.cpp server did not start ({0}), and Docker did not either: {1}" -f $reason, $_.Exception.Message)
}
Save-Runtime -Runtime 'docker' -Reason $reason
exit 0
