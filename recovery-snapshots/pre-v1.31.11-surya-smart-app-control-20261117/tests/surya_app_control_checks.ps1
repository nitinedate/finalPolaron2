# V1.31.10 offline checks: when Windows App Control blocks llama-server.exe, scripts\ensure_host_surya.ps1
# runs the same llama.cpp server in Docker. Docker, Start-Process, the HTTP probe and the Python bootstrap
# are simulated; the real manager script runs.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }

$manager = Join-Path $ProjectRoot 'scripts/ensure_host_surya.ps1'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('polaron-surya-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

# --- simulated environment ---------------------------------------------------------------------------
$global:SimDocker = @{ calls = New-Object System.Collections.Generic.List[string]; running = $false; failImage = ''; nvidia = $false }
$global:SimStart = @{ mode = 'blocked'; calls = 0 }
function global:docker {
    $line = ($args | ForEach-Object { [string]$_ }) -join ' '
    $global:SimDocker.calls.Add($line)
    $global:LASTEXITCODE = 0
    switch ([string]$args[0]) {
        'info' { if ($line -match 'Runtimes') { if ($global:SimDocker.nvidia) { return '{"nvidia":{},"runc":{}}' } return '{"runc":{}}' } return '27.0.0' }
        'inspect' { if ($global:SimDocker.running) { return 'true' } $global:LASTEXITCODE = 1; return '' }
        'rm' { $global:SimDocker.running = $false; return }
        'pull' { return }
        'run' {
            if ($global:SimDocker.failImage -and $line -match [regex]::Escape($global:SimDocker.failImage)) { $global:LASTEXITCODE = 125; return }
            $global:SimDocker.running = $true; return 'c0ffee'
        }
        'logs' { return 'llama server log' }
    }
}
function global:Invoke-RestMethod {
    param([string]$Uri, $TimeoutSec, $ErrorAction)
    if ($global:SimDocker.running -or $global:SimStart.mode -eq 'native_ok_running') { return [pscustomobject]@{ data = @([pscustomobject]@{ id = 'datalab-to/surya-ocr-2.5' }) } }
    throw 'connection refused'
}
function global:Start-Process {
    param($FilePath, $ArgumentList, [switch]$PassThru, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
    $global:SimStart.calls++
    if ($global:SimStart.mode -eq 'blocked') { throw 'This command cannot be run due to the error: An Application Control policy has blocked this file.' }
    if ($global:SimStart.mode -eq 'missing') { throw 'This command cannot be run due to the error: The system cannot find the file specified.' }
    if ($global:SimStart.mode -eq 'image_hash') {
        return [pscustomobject]@{ Id = 4242; HasExited = $true; ExitCode = -1073740760 } | Add-Member -MemberType ScriptMethod -Name Refresh -Value { } -PassThru
    }
    $global:SimStart.mode = 'native_ok_running'
    return [pscustomobject]@{ Id = 4243; HasExited = $false; ExitCode = 0 } | Add-Member -MemberType ScriptMethod -Name Refresh -Value { } -PassThru
}
function global:Start-Sleep { param([int]$Seconds, [int]$Milliseconds) }

$cache = Join-Path $tmp 'cache'
$env:SURYA_HOST_CACHE_DIR = $cache
$env:LOCALAPPDATA = $tmp
$models = Join-Path $cache 'models'
New-Item -ItemType Directory -Force -Path $models | Out-Null
'gguf' | Set-Content -LiteralPath (Join-Path $models 'surya-2.5.gguf')
'gguf' | Set-Content -LiteralPath (Join-Path $models 'surya-2.5-mmproj.gguf')
'exe' | Set-Content -LiteralPath (Join-Path $cache 'llama-server.exe')
New-Item -ItemType File -Force -Path (Join-Path $cache '.surya-host-packages-v2') | Out-Null
# The manager runs <cache>\python\Scripts\python.exe; here it is a small script that answers like the bootstrap.
$py = Join-Path (Join-Path $cache 'python') 'Scripts\python.exe'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $py) | Out-Null
$bootLog = Join-Path $tmp 'bootstrap-args.txt'
@"
#!/usr/bin/env bash
echo "`$*" >> '$bootLog'
out=""; modelonly=0
while [ `$# -gt 0 ]; do case "`$1" in --result-file) out="`$2"; shift;; --model-only) modelonly=1;; esac; shift; done
server='$(Join-Path $cache 'llama-server.exe')'; [ `$modelonly = 1 ] && server=''
printf '{"model":"%s","mmproj":"%s","server":"%s"}' '$(Join-Path $models 'surya-2.5.gguf')' '$(Join-Path $models 'surya-2.5-mmproj.gguf')' "`$server" > "`$out"
"@ | Set-Content -LiteralPath $py
& chmod +x $py
$runtimeFile = Join-Path $cache 'surya-runtime.json'

function Invoke-Manager {
    $global:SimDocker.calls.Clear()
    $global:SimStart.calls = 0
    $err = ''
    try { & $manager -ProjectRoot $ProjectRoot *> $null } catch { $err = [string]$_.Exception.Message }
    return $err
}
function Get-RunLine { return @($global:SimDocker.calls | Where-Object { $_ -like 'run *' }) }

try {
    # 1. App Control blocks llama-server.exe: the same server runs in Docker (CPU image, no NVIDIA here).
    Remove-Item Env:SURYA_HOST_RUNTIME -ErrorAction SilentlyContinue
    $err = Invoke-Manager
    Assert-True ($err -eq '') "The block is handled, not reported as a startup error ($err)"
    $run = @(Get-RunLine)
    Assert-True ($global:SimStart.calls -eq 1 -and $run.Count -eq 1) 'One native attempt, then one container'
    Assert-True ($run[0] -match 'ghcr\.io/ggml-org/llama\.cpp:server ' -and $run[0] -notmatch '--gpus') 'Without NVIDIA the CPU image is used'
    Assert-True ($run[0] -match '--name polaron-surya-ocr' -and $run[0] -match '--restart unless-stopped' -and $run[0] -match '-p 8000:8000') 'Polaron container on port 8000, restarted with Docker'
    Assert-True ($run[0].Contains($models + ':/models:ro')) 'The cached weights are mounted read-only'
    Assert-True ($run[0] -match '--model /models/surya-2\.5\.gguf' -and $run[0] -match '--mmproj /models/surya-2\.5-mmproj\.gguf' -and $run[0] -match '--alias datalab-to/surya-ocr-2\.5' -and $run[0] -match '--n-gpu-layers 0') 'Same model, projector and model ID as the native server'
    $saved = Get-Content -LiteralPath $runtimeFile -Raw | ConvertFrom-Json
    Assert-True ($saved.runtime -eq 'docker' -and $saved.reason -match 'Application Control') 'The choice is remembered with its reason'

    # 2. Next launch: straight to Docker, weights only (no llama.cpp download, no blocked attempt).
    $global:SimDocker.running = $false
    Remove-Item -LiteralPath $bootLog -ErrorAction SilentlyContinue
    $err = Invoke-Manager
    Assert-True ($err -eq '' -and $global:SimStart.calls -eq 0 -and @(Get-RunLine).Count -eq 1) 'A remembered Docker runtime skips the native attempt'
    Assert-True ((Get-Content -LiteralPath $bootLog -Raw) -match '--model-only') 'Only the model weights are provisioned for Docker'

    # 3. A container Docker restarted on its own is reused, not recreated.
    $global:SimDocker.running = $true
    $err = Invoke-Manager
    Assert-True ($err -eq '' -and @(Get-RunLine).Count -eq 0 -and -not ($global:SimDocker.calls -match '^rm ')) 'A ready container is kept'

    # 4. NVIDIA GPU: CUDA image with --gpus all; if that image fails, the CPU image is used.
    Remove-Item -LiteralPath $runtimeFile -Force
    $global:SimDocker.running = $false; $global:SimDocker.nvidia = $true; $global:SimStart.mode = 'blocked'
    $err = Invoke-Manager
    $run = @(Get-RunLine)
    Assert-True ($err -eq '' -and $run.Count -eq 1 -and $run[0] -match 'llama\.cpp:server-cuda' -and $run[0] -match '--gpus all' -and $run[0] -match '--n-gpu-layers 12') 'NVIDIA: the CUDA image with the configured GPU layers'
    Remove-Item -LiteralPath $runtimeFile -Force
    $global:SimDocker.running = $false; $global:SimDocker.failImage = 'server-cuda'
    $err = Invoke-Manager
    $run = @(Get-RunLine)
    Assert-True ($err -eq '' -and $run.Count -eq 2 -and $run[1] -match 'llama\.cpp:server ' -and $run[1] -notmatch '--gpus') 'A failing CUDA container falls back to CPU'
    $global:SimDocker.failImage = ''; $global:SimDocker.nvidia = $false

    # 5. A DLL refused at load (process ends with STATUS_INVALID_IMAGE_HASH) is the same block.
    Remove-Item -LiteralPath $runtimeFile -Force
    $global:SimDocker.running = $false; $global:SimStart.mode = 'image_hash'
    $err = Invoke-Manager
    Assert-True ($err -eq '' -and @(Get-RunLine).Count -eq 1) 'Exit 0xC0000428 is treated as an App Control block'

    # 6. SURYA_HOST_RUNTIME=native: no Docker, a clear error.
    Remove-Item -LiteralPath $runtimeFile -Force
    $global:SimDocker.running = $false; $global:SimStart.mode = 'blocked'; $env:SURYA_HOST_RUNTIME = 'native'
    $err = Invoke-Manager
    Assert-True ($err -match 'App Control' -and $err -match 'SURYA_HOST_RUNTIME' -and @(Get-RunLine).Count -eq 0) 'native: the block is reported with the way out'
    Remove-Item Env:SURYA_HOST_RUNTIME

    # 7. Any other start failure is reported as before (no silent switch to Docker).
    $global:SimStart.mode = 'missing'
    $err = Invoke-Manager
    Assert-True ($err -match 'cannot find the file' -and @(Get-RunLine).Count -eq 0 -and -not (Test-Path -LiteralPath $runtimeFile)) 'Other errors are not taken for App Control'

    # 8. The native server still works where it is allowed.
    $global:SimStart.mode = 'native_ok'
    $err = Invoke-Manager
    Assert-True ($err -eq '' -and $global:SimStart.calls -eq 1 -and @(Get-RunLine).Count -eq 0) 'Allowed native server: no Docker'
    $global:SimStart.mode = 'blocked'

    $bytes = [IO.File]::ReadAllBytes($manager)
    Assert-True (@($bytes | Where-Object { $_ -gt 127 }).Count -eq 0) 'The manager is plain ASCII (Windows PowerShell 5.1)'

    # 9. The launcher starts Polaron without OCR instead of stopping (unless SURYA_REQUIRED=true).
    $launcher = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'script_docker/start_docker.ps1'))
    Assert-True ($launcher -match 'catch \{\s+\$suryaError = \[string\]\$_\.Exception\.Message' -and $launcher -match "SURYA_REQUIRED -match" -and $launcher -match 'Polaron starts WITHOUT OCR') 'The launcher catches an OCR failure and continues'
    Assert-True ($launcher -match "-ne 'disabled' -and -not \`$script:SuryaUnavailable") 'The OCR reachability check is skipped when OCR did not start'
    $align = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'scripts/align_surya_env.ps1'))
    Assert-True ($align -match "'SURYA_HOST_RUNTIME'" -and $align -match "'SURYA_REQUIRED'") 'The .env Surya settings reach the manager'
    Write-Output "PASS: $script:checks Surya App Control checks"
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    foreach ($n in @('docker', 'Invoke-RestMethod', 'Start-Process', 'Start-Sleep')) { Remove-Item -LiteralPath ("Function:\" + $n) -ErrorAction SilentlyContinue }
}
