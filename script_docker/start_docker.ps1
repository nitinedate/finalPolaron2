# Aetheris / Polaron Windows Docker launcher.
# Windows PowerShell 5.1 compatible.
# Public profiles validate certificates and start the HTTPS edge before product stacks.

[CmdletBinding()]
param(
    [ValidateSet('local', 'nitin', 'prod')]
    [string]$Profile = 'local',
    [string]$PublicIP = '',
    [string]$Domain = '',
    [string]$AcmeEmail = '',
    [switch]$PrepareOnly,
    [switch]$ValidateOnly,
    [switch]$RefreshDependencies,
    [switch]$AutoRecoverEngine,
    [switch]$NoCache,
    [switch]$Recreate,
    [switch]$HandoverLegacyOpenSearch
)

$ErrorActionPreference = 'Stop'
# PowerShell 7 otherwise turns a missing Docker network (exit 1) into a terminating
# error before the launcher can create aetheris-common.
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
$libPath = Join-Path $scriptDir 'start_docker.lib.ps1'

if (-not (Test-Path -LiteralPath $libPath -PathType Leaf)) {
    Write-Host "ERROR: Missing startup library: $libPath" -ForegroundColor Red
    Write-Host 'Extract the complete project ZIP and retry.' -ForegroundColor Yellow
    exit 2
}

. $libPath

function Write-Ok([string]$Message) { Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    $Message" -ForegroundColor Yellow }

function Get-RunningContainerIds {
    $ids = @(& docker ps -q 2>$null)
    return @($ids | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() })
}

function Stop-PolaronStacks {
    # Stop only owned project containers; never run global docker stop/prune.
    # V1.10.8.1: all Polaron containers are stopped in ONE parallel docker stop
    # (each still gets 45 s to shut down cleanly), with progress every 10 s.
    # Before, the six projects were stopped one after another and the console
    # showed nothing while e.g. the 11 vulnerability containers shut down.
    Write-Step 'Stop Polaron application containers only (preserve shared services and evidence)'
    # Do not stop Podman/other project infrastructure just because the name starts
    # with aetheris. Only Docker Compose projects owned by Polaron are in scope.
    # Greenbone runtime/feed services and the HTTPS certificate renewer are
    # long-lived dependencies; restarting them on every source change wastes time.
    $names = @('aetheris-gateway','aetheris-vuln','aetheris-mobile-malware','aetheris-mobile-ios','aetheris-mobile-android','aetheris-forensic')
    $all = New-Object System.Collections.ArrayList
    foreach ($name in $names) {
        $ids = @()
        if ($name -eq 'aetheris-gateway') {
            $ids = @(& docker ps -q --filter "label=com.docker.compose.project=$name" --filter 'label=com.docker.compose.service=gateway')
        } elseif ($name -eq 'aetheris-vuln') {
            foreach ($service in @('api','worker-nessus')) {
                $ids += @(& docker ps -q --filter "label=com.docker.compose.project=$name" --filter "label=com.docker.compose.service=$service")
                if ($LASTEXITCODE -ne 0) { throw "Cannot inspect $name/$service" }
            }
        } else {
            $ids = @(& docker ps -q --filter "label=com.docker.compose.project=$name")
        }
        if ($LASTEXITCODE -ne 0) { throw "Cannot query owned containers for $name" }
        $ids = @($ids | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() } | Sort-Object -Unique)
        if ($ids.Count -gt 0) {
            Write-Host "    $name : $($ids.Count) application container(s)"
            foreach ($id in $ids) { if (-not $all.Contains($id)) { [void]$all.Add($id) } }
        }
    }
    if ($all.Count -eq 0) {
        Write-Ok 'No Polaron containers are running.'
        return
    }
    Write-Host ("    Stopping {0} containers together (each gets up to 45 s to shut down cleanly)..." -f $all.Count)
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $proc = Start-Process -FilePath 'docker' -ArgumentList (@('stop', '--time', '45') + @($all)) -NoNewWindow -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $null = $proc.Handle  # keeps ExitCode readable after exit (Windows PowerShell 5.1)
        $hardLimitSec = 150
        if ("$env:AETHERIS_STOP_HARD_LIMIT_SEC" -match '^[0-9]+$' -and [int]$env:AETHERIS_STOP_HARD_LIMIT_SEC -gt 0) {
            $hardLimitSec = [int]$env:AETHERIS_STOP_HARD_LIMIT_SEC
        }
        while (-not $proc.WaitForExit(10000)) {
            $running = @(Get-RunningContainerIds)
            $left = @($all | Where-Object { $running -contains $_ })
            Write-Host ("    ... {0:n0} s, {1} of {2} still shutting down" -f $sw.Elapsed.TotalSeconds, $left.Count, $all.Count)
            if ($sw.Elapsed.TotalSeconds -ge $hardLimitSec) {
                Write-Warn ("docker stop did not return after {0} s; forcing the remaining {1} container(s) to stop." -f $hardLimitSec, $left.Count)
                try { $proc.Kill() } catch { }
                if ($left.Count) { & docker kill @left *> $null }
                break
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
    $running = @(Get-RunningContainerIds)
    $left = @($all | Where-Object { $running -contains $_ })
    if ($left.Count) {
        throw ("Cannot stop {0} Polaron container(s) ({1}); refusing to start competing containers. Restart Docker Desktop and retry." -f $left.Count, ($left -join ', '))
    }
    Write-Ok ("Stopped {0} containers in {1:n0} s." -f $all.Count, $sw.Elapsed.TotalSeconds)
}

function Assert-StartupAssets {
    # Validate the exact gateway files used by the configured profile.
    param([bool]$Public)
    $files = if ($Public) {
        @('deployment/windows-ip-https/docker-compose.gateway-public-https.yml',
          'deployment/windows-ip-https/nginx.gateway.https.conf')
    } else {
        @('deployment/windows-ip-https/docker-compose.gateway-local.yml')
    }
    foreach ($file in $files) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $file) -PathType Leaf)) {
            throw "Required gateway asset missing: $file. Restore the complete deployment directory before restarting containers."
        }
    }
}

function Assert-RuntimeHealth {
    Write-Step 'Verify gateway, API health, and evidence drive mounts'
    $required = @(
        @{ Project='aetheris-common'; Service='redis' },
        @{ Project='aetheris-forensic'; Service='api' },
        @{ Project='aetheris-mobile-android'; Service='api' },
        @{ Project='aetheris-mobile-ios'; Service='api' },
        @{ Project='aetheris-mobile-malware'; Service='api' }
    )
    foreach ($row in $required) {
        if (-not (Wait-ComposeServiceReady -ProjectName $row.Project -Service $row.Service -TimeoutSeconds 120)) {
            throw "Service not ready: $($row.Project)/$($row.Service). Check docker logs and proxy upstream."
        }
    }
    $gatewayState = Get-ComposeServiceContainerState -ProjectName 'aetheris-gateway' -Service 'gateway'
    if (-not $gatewayState -or $gatewayState.Status -ne 'running') {
        throw 'Gateway is not running. API polling would return 502.'
    }
    $mountConfig = Join-Path $root 'docker-compose.drives.generated.yml'
    if (Test-Path -LiteralPath $mountConfig) {
        $content = Get-Content -LiteralPath $mountConfig -Raw
        if (-not $content.Trim() -or $content.Trim() -eq 'services:') {
            throw 'Generated drive mount configuration is empty; Windows evidence drives would be inaccessible.'
        }
    } else {
        throw 'Missing generated drive override. Check drive generator and disk availability.'
    }
    # Validate from inside the API rather than trusting frontend's static Online badge.
    $apiId = @(& docker ps -q --filter 'label=com.docker.compose.project=aetheris-mobile-android' --filter 'label=com.docker.compose.service=api')
    if ($apiId.Count -lt 1 -or -not $apiId[0]) { throw 'Android API container not found.' }
    $probe = @(& docker exec $apiId[0] python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=8).read()" 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Android API /health failed: $($probe -join ' ')" }
    Write-Ok 'Gateway, APIs and generated drive mapping validated.'
}

function Invoke-DriveMountRefresh {
    # Refresh ALL split-stack overrides with a single verified Windows/Docker
    # inventory. Generic docker-compose.drives.generated.yml is NOT consumed by
    # Get-StackComposeArgs for forensic or mobile stacks.
    # A timed-out or failed scan MUST NOT reuse yesterday's G:/ mapping.
    param([string]$Generator, [bool]$Public)
    $timeoutSec = 240
    if ("$env:AETHERIS_DRIVE_SCAN_TIMEOUT_SEC" -match '^[0-9]+$' -and [int]$env:AETHERIS_DRIVE_SCAN_TIMEOUT_SEC -gt 0) {
        $timeoutSec = [int]$env:AETHERIS_DRIVE_SCAN_TIMEOUT_SEC
    }
    $required = @(
        'docker-compose.drives.generated.yml',
        'docker-compose.drives.forensic.yml',
        'docker-compose.drives.mobile-android.yml',
        'docker-compose.drives.mobile-ios.yml',
        'docker-compose.drives.mobile-malware.yml'
    )
    $startedUtc = [DateTime]::UtcNow.AddSeconds(-2)
    Write-Host ("    Scanning drives for ALL product stacks (time limit {0}s)..." -f $timeoutSec)
    $proc = Start-Process -FilePath 'powershell.exe' -NoNewWindow -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $Generator),
        '-WriteStartupStackOverrides')
    $null = $proc.Handle  # keeps ExitCode readable after exit in PowerShell 5.1
    if (-not $proc.WaitForExit($timeoutSec * 1000)) {
        & taskkill.exe /PID $proc.Id /T /F *> $null
        throw ("Drive scan timed out after {0}s. Refusing stale drive mappings (e.g. disconnected G:). Check/remove offline USB/network drives or raise AETHERIS_DRIVE_SCAN_TIMEOUT_SEC, then retry." -f $timeoutSec)
    }
    if ($proc.ExitCode -ne 0) {
        throw ("Drive scan failed (exit {0}). No stale drive override will be reused. Reconnect required evidence disks or inspect the scan output." -f $proc.ExitCode)
    }
    foreach ($file in $required) {
        $path = Join-Path $root $file
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Drive scan did not produce required product override: $file"
        }
        $item = Get-Item -LiteralPath $path
        $content = Get-Content -LiteralPath $path -Raw
        if ($item.LastWriteTimeUtc -lt $startedUtc -or
            -not $content -or $content -notmatch '(?m)^services:\s*$' -or
            $content -notmatch '(?m)^\s+target:') {
            throw "Drive override is stale or empty: $file. Refusing the previous G: bind configuration."
        }
    }
    $forensicFile = Join-Path $root 'docker-compose.drives.forensic.yml'
    $forensicText = Get-Content -LiteralPath $forensicFile -Raw
    $mapped = @([regex]::Matches($forensicText, "/host/([a-z])'") |
        ForEach-Object { $_.Groups[1].Value.ToUpper() } | Sort-Object -Unique)
    Write-Ok ("Current forensic drive mounts: {0}" -f $(if ($mapped.Count) { $mapped -join ', ' } else { 'none' }))
    Write-Ok 'Generic, forensic, Android, iOS and Malware Detection drive overlays refreshed from one verified scan.'
}

function Show-NewLogLines {
    # Print the complete lines a running process has added to its log since the last call; returns the new count.
    # -Final also prints a last line that has no newline yet.
    param([string]$Path, [int]$Shown, [switch]$Final)
    if (-not (Test-Path -LiteralPath $Path)) { return $Shown }
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try { $text = (New-Object System.IO.StreamReader($fs)).ReadToEnd() } finally { $fs.Dispose() }
    } catch { return $Shown }
    if (-not $text) { return $Shown }
    $lines = @($text -split "`r?`n")
    if ($text.EndsWith("`n") -or -not $Final) { $lines = @($lines | Select-Object -First ([Math]::Max(0, $lines.Count - 1))) }
    for ($i = $Shown; $i -lt $lines.Count; $i++) {
        if ([string]$lines[$i]) { Write-Host ('    ' + $lines[$i]) }
    }
    return [Math]::Max($Shown, $lines.Count)
}

function Ensure-WorkstationHelper {
    # Connected phones (detection, Device -> Image, and Malware Detection's "Scan a connected phone") need the
    # Polaron host helper and the examiner kit (tools\host-python with pymobiledevice3, tools\platform-tools\adb)
    # on this workstation. ensure-host-drive-helper.ps1 installs the kit when missing and restarts a helper that
    # is older than this release, so a deployment never leaves phones on yesterday's helper. Never fatal.
    Write-Step 'Ensure the workstation helper for connected phones (detection and direct malware scans)'
    $ensure = Join-Path $root 'scripts/ensure-host-drive-helper.ps1'
    if (-not (Test-Path -LiteralPath $ensure -PathType Leaf)) {
        Write-Warn "Missing $ensure; connected phones cannot be detected or scanned directly."
        return
    }
    # Wait for the setup script to EXIT, never for its output to END: the helper it starts keeps running in the
    # background and inherits the script's output handles, so piping that output (| ForEach-Object) waits forever.
    # The output goes to a log file instead, shown line by line while the script runs.
    $log = Join-Path ([System.IO.Path]::GetTempPath()) ('aetheris-helper-setup-' + [guid]::NewGuid().ToString('N') + '.log')
    $errLog = $log + '.err'
    $code = $null
    try {
        $proc = Start-Process -FilePath 'powershell.exe' -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError $errLog `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $ensure))
        $null = $proc.Handle  # keeps ExitCode readable after exit in Windows PowerShell 5.1
        $deadline = (Get-Date).AddMinutes(15)  # first run may download the examiner kit
        $shown = 0
        while (-not $proc.HasExited -and (Get-Date) -lt $deadline) {
            $shown = Show-NewLogLines -Path $log -Shown $shown
            Start-Sleep -Milliseconds 500
        }
        $shown = Show-NewLogLines -Path $log -Shown $shown -Final
        $null = Show-NewLogLines -Path $errLog -Shown 0 -Final
        if ($proc.HasExited) { $code = $proc.ExitCode } else { Write-Warn "Helper setup is still running after 15 minutes; continuing. Its log: $log" }
    } catch {
        Write-Warn "Could not run the helper setup: $($_.Exception.Message)"
    }
    $port = 9876
    if ("$env:HOST_DRIVE_HELPER_PORT" -match '^[0-9]+$') { $port = [int]$env:HOST_DRIVE_HELPER_PORT }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 10
        $running = [string]$health.helper_version
        $onDisk = $null
        $helperFile = Join-Path $root 'scripts/host-drive-helper.ps1'
        if (Test-Path -LiteralPath $helperFile) {
            $m = Select-String -LiteralPath $helperFile -Pattern '\$script:HelperVersion\s*=\s*"([0-9.]+)"' | Select-Object -First 1
            if ($m) { $onDisk = $m.Matches[0].Groups[1].Value }
        }
        if ($health.malware_triage) {
            Write-Ok "Workstation helper v$running is running: phones are detected and can be scanned directly."
        } elseif ($onDisk -and $running -and $onDisk -ne $running) {
            # The helper is running an old version even though a newer script is on disk here: the setup could not
            # replace the running one (another Polaron folder, or it is launched from elsewhere).
            Write-Warn "Workstation helper is still v$running but this folder has v$onDisk. The old helper did not get replaced."
            Write-Warn "Run scripts\ensure-host-drive-helper.ps1 -ForceRestart on the workstation. If it stays old, another"
            Write-Warn "Polaron folder is running the helper - close it (or restart the workstation) and run from this folder."
        } elseif ($onDisk -and $onDisk -ne '5.55' -and -not $health.malware_triage) {
            Write-Warn "This Polaron folder's helper is v$onDisk, older than this release. Overlay the latest patch onto THIS folder, then run start_docker again."
        } else {
            Write-Warn "Workstation helper v$running cannot read phones for Malware Detection yet. Run scripts\ensure-host-drive-helper.ps1 -ForceRestart."
        }
    } catch {
        Write-Warn "Workstation helper is not answering (setup exit $code). Phone detection and Scan a connected phone need it: run scripts\ensure-host-drive-helper.ps1."
    }
}

function Assert-DockerReady {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        throw 'Docker CLI was not found. Install/start Docker Desktop in Linux container mode.'
    }
    . (Join-Path $root 'scripts/docker-engine-recovery.ps1')
    . (Join-Path $root 'scripts/assert-docker-ready.ps1')
    Assert-PolaronDockerReady -Docker $docker.Source -AutoRecoverEngine:$AutoRecoverEngine
    Write-Ok 'Docker Desktop Linux engine and Compose are ready.'
}

function Get-ComposeConfig {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArgs)
    $raw = & docker @ComposeArgs config --format json
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose validation failed: docker $($ComposeArgs -join ' ') config" }
    try { return (($raw -join "`n") | ConvertFrom-Json) }
    catch { throw "Docker Compose returned invalid JSON while validating: docker $($ComposeArgs -join ' ') config" }
}

function Get-AllStackDefinitions {
    $rows = @()
    foreach ($name in @('common', 'forensic', 'mobile-android', 'mobile-ios', 'mobile-malware', 'vuln')) {
        $rows += [pscustomobject]@{ Name = $name; Args = @(Get-StackComposeArgs -Root $root -Name $name) }
    }
    $gateway = @('compose', '--project-directory', $root, '--project-name', 'aetheris-gateway',
        '-f', 'services/gateway/docker-compose.yml',
        '-f', 'deployment/windows-ip-https/docker-compose.gateway-local.yml')
    $rows += [pscustomobject]@{ Name = 'gateway'; Args = $gateway }
    return @($rows)
}

function Assert-AllComposeFiles {
    param([switch]$CreateRuntimeDirectories)
    Write-Step 'Validate product Compose files and bind mounts'
    foreach ($stack in @(Get-AllStackDefinitions)) {
        $cfg = Get-ComposeConfig -ComposeArgs $stack.Args
        Assert-DeploymentComposePaths -Root $root -Config $cfg -ValidateOnly:(-not $CreateRuntimeDirectories)
        Write-Ok "$($stack.Name) Compose configuration is valid."
    }

    # Public gateway is a separate validation path because it references the generated
    # certificate-backed nginx config. Assert-DeploymentComposePaths explicitly permits it.
    foreach ($publicProfile in @('nitin', 'prod')) {
        $args = @('compose', '--project-directory', $root, '--project-name', 'aetheris-gateway',
            '-f', 'services/gateway/docker-compose.yml',
            '-f', 'deployment/windows-ip-https/docker-compose.gateway-public-https.yml')
        $cfg = Get-ComposeConfig -ComposeArgs $args
        Assert-DeploymentComposePaths -Root $root -Config $cfg -ValidateOnly:(-not $CreateRuntimeDirectories)
    }
}

function Invoke-StackBuild {
    param([string]$Name, [string[]]$ComposeArgs, [string]$BuildId)
    $args = @($ComposeArgs) + @(Get-AppBuildArgs -BuildId $BuildId -RefreshDependencies:($RefreshDependencies -or $NoCache))
    Invoke-Docker -Title "Build/compile $Name" -DockerArgs $args
}

function Get-ProdDownComposeArgs {
    param([string]$Name, [string[]]$ComposeArgs)
    # Profiles keep Greenbone, ZAP, Trivy, and the certificate bootstrap in the
    # same project. Down without those profiles would leave them running.
    $extra = @()
    if ($Name -eq 'vuln') {
        $extra = @('--profile', 'gvm', '--profile', 'vuln-scanners', '--profile', 'legacy-ui')
    } elseif ($Name -eq 'gateway') {
        $extra = @('--profile', 'acme-bootstrap', '--profile', 'certbot')
    } elseif ($Name -eq 'mobile-malware') {
        $extra = @('--profile', 'gpu')
    }
    return @($ComposeArgs) + $extra
}

function Invoke-DownPolaronProjects {
    # compose down removes containers and the project network. It does not
    # delete volumes unless -v is passed. Evidence and database volumes stay.
    Write-Step 'Down all Polaron Compose projects (containers removed; volumes and evidence kept)'
    $stacks = @(Get-AllStackDefinitions)
    foreach ($stack in $stacks) {
        # Reusable infrastructure is owned outside the Polaron product lifecycle.
        if ($stack.Name -eq 'common') { continue }
        $args = @($stack.Args)
        if ($stack.Name -eq 'gateway') { $args = @(Get-PublicGatewayArgs) }
        $downArgs = @(Get-ProdDownComposeArgs -Name $stack.Name -ComposeArgs $args) + @('down', '--remove-orphans')
        Invoke-Docker -Title "Down $($stack.Name)" -DockerArgs $downArgs
    }
    Write-Ok 'Polaron containers are down. Named volumes were not removed.'
}

function Test-DockerImagePresent {
    param([string]$Image)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker image inspect $Image --format '{{.Id}}' 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Ensure-CachedMinioImageAlias {
    # If an earlier deployment already has Chainguard MinIO from cgr.dev,
    # re-tag it locally for the Docker Hub name. This is purely local and
    # does not alter the bind-mounted evidence/object data.
    if ($env:COMMON_MINIO_IMAGE -and $env:COMMON_MINIO_IMAGE -ne 'chainguard/minio:latest') { return }
    if (Test-DockerImagePresent -Image 'chainguard/minio:latest') { return }
    if (Test-DockerImagePresent -Image 'cgr.dev/chainguard/minio:latest') {
        Write-Host '    Reusing locally cached Chainguard MinIO under its Docker Hub image name.'
        Invoke-Docker -Title 'Tag cached MinIO image (no download)' -DockerArgs @('tag','cgr.dev/chainguard/minio:latest','chainguard/minio:latest')
    }
}

function Remove-LegacyGpuSidecars {
    # Previous releases ran one permanent nvidia-cuda service per product.
    # Remove only these unused, product-owned GPU probe containers. This does
    # not affect shared Docker, the host NVIDIA driver, or GPU worker containers.
    foreach ($project in @('aetheris-forensic','aetheris-mobile-android','aetheris-mobile-ios','aetheris-mobile-extract')) {
        $ids = @(& docker ps -aq --filter "label=com.docker.compose.project=$project" --filter 'label=com.docker.compose.service=nvidia-cuda')
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect legacy GPU sidecars for $project" }
        foreach ($id in @($ids | Where-Object { $_ -and $_.Trim() })) {
            $code = Invoke-DockerNativeSafe -DockerArgs @('rm','-f',([string]$id).Trim()) -Quiet
            if ($code -ne 0) { Write-Warn "Could not remove obsolete GPU probe $id from $project" }
        }
    }
}

function Test-MinIOHttpReady {
    $port = 9004
    if ("$env:COMMON_MINIO_PORT" -match '^\d{2,5}$') { $port = [int]$env:COMMON_MINIO_PORT }
    else {
        $envFile = Join-Path $root '.env'
        if (Test-Path -LiteralPath $envFile -PathType Leaf) {
            foreach ($line in @(Get-Content -LiteralPath $envFile)) {
                if ($line -match '^\s*COMMON_MINIO_PORT\s*=\s*(\d+)') { $port = [int]$Matches[1]; break }
            }
        }
    }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/minio/health/live" -TimeoutSec 3
        return ($response.StatusCode -eq 200)
    } catch { return $false }
}

function Wait-MinIOHttpReady {
    $limit = (Get-Date).AddSeconds(90)
    do {
        if (Test-MinIOHttpReady) { return }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $limit)
    throw 'Shared MinIO is not responding on its health endpoint. Check docker logs for the aetheris-common MinIO service and verify COMMON_MINIO_PORT.'
}

function Test-SharedInfrastructureRunning {
    # Do not touch common services belonging to other projects on routine launch.
    foreach ($service in @('postgres', 'redis', 'minio', 'opensearch', 'pgadmin', 'mailhog')) {
        $state = Get-ComposeServiceContainerState -ProjectName 'aetheris-common' -Service $service
        if (-not $state -or $state.Status -ne 'running' -or $state.Health -in @('unhealthy', 'starting')) {
            return $false
        }
    }
    return (Test-MinIOHttpReady)
}

function Test-ApplicationImagesCurrent {
    param([string]$BuildId)
    # All tags below are produced by the shared backend / frontend builds.
    # Inspect local image LABELs; never query a remote registry to decide whether
    # a previously successful build can be reused.
    $required = @(
        'rag_new2-api',
        'aetheris-mobile-android-app',
        'aetheris-mobile-ios-app',
        'aetheris-mobile-malware-app',
        'aetheris-vuln-worker-nessus',
        'aetheris-gateway',
        'aetheris-mobile-android-frontend',
        'aetheris-mobile-ios-frontend'
    )
    if (-not (Test-DockerImagePresent -Image 'aetheris-mobile-malware-clamav')) {
        Write-Host '    missing: aetheris-mobile-malware-clamav'
        return $false
    }
    $saved = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        foreach ($image in $required) {
            $label = @(& docker image inspect --format '{{ index .Config.Labels "org.aetheris.application-build" }}' $image 2>$null)
            if ($LASTEXITCODE -ne 0 -or -not $label -or ([string]$label[0]).Trim() -ne $BuildId) {
                Write-Host "    image stale or absent: $image"
                return $false
            }
        }
        return $true
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Get-StartupRequiredImages {
    # Active (non-profiled) third-party service images and build-stage bases.
    # No global 30+ image prefetch: scanner feeds are optional and may be many GB.
    $names = New-Object System.Collections.Generic.List[string]
    foreach ($base in @(
        'nvidia/cuda:12.6.3-runtime-ubuntu24.04',
        'python:3.12-slim',
        'node:20-alpine',
        'nginx:1.27-alpine',
        'aquasec/trivy:0.58.1',
        'clamav/clamav:1.4'
    )) { if (-not $names.Contains($base)) { [void]$names.Add($base) } }

    foreach ($stack in @(Get-AllStackDefinitions)) {
        $composeArgs = @($stack.Args)
        if ($stack.Name -eq 'gateway' -and $isPublic) {
            $composeArgs = @(Get-PublicGatewayArgs)
        }
        $cfg = Get-ComposeConfig -ComposeArgs $composeArgs
        foreach ($entry in @($cfg.services.PSObject.Properties)) {
            $svc = $entry.Value
            if (-not $svc -or -not $svc.image) { continue }
            # A Docker Compose profile is not automatically started; do not pull
            # optional Greenbone feeds or GPU diagnostic images during preflight.
            if ($svc.PSObject.Properties.Name -contains 'profiles' -and @($svc.profiles).Count -gt 0) { continue }
            if ([string]$svc.image -match '^(aetheris-|rag_new2-)') { continue }
            if ($svc.PSObject.Properties.Name -contains 'build' -and $null -ne $svc.build) { continue }
            $image = ([string]$svc.image).Trim()
            if ($image -and -not $names.Contains($image)) { [void]$names.Add($image) }
        }
    }
    return @($names)
}

function Ensure-StartupImageCache {
    # Cache preflight BEFORE shutting down case-processing workers. Missing base
    # images are installed once; already-present image tags are never re-pulled.
    Write-Step 'Check cached Docker bases and required package images'
    Ensure-CachedMinioImageAlias
    $missing = New-Object System.Collections.Generic.List[string]
    $present = 0
    foreach ($image in @(Get-StartupRequiredImages)) {
        if (Test-DockerImagePresent -Image $image) { $present++ }
        else { [void]$missing.Add($image) }
    }
    Write-Ok ("Docker image cache: {0} present, {1} missing" -f $present, $missing.Count)
    if ($missing.Count -eq 0) {
        Write-Ok 'All required image packages are cached. No registry downloads are needed.'
    } else {
        Write-Warn 'First-run/missing packages detected. Fetching only missing images before stopping Polaron.'
        foreach ($image in @($missing)) {
            Invoke-Docker -Title "Download missing image once: $image" -DockerArgs @('pull', $image)
            if (-not (Test-DockerImagePresent -Image $image)) {
                throw "Required image $image is still missing. Existing application containers remain running."
            }
        }
    }
    foreach ($tag in @('aetheris-backend-deps','aetheris-frontend-deps')) {
        if (Test-DockerImagePresent -Image $tag) {
            Write-Host "    package stage cached: $tag"
        } else {
            Write-Host "    package stage not tagged yet: $tag (BuildKit will install only uncached dependencies)"
        }
    }
}

function Add-UniqueImageName {
    param($List, [string]$Image)
    $name = ([string]$Image).Trim()
    if (-not $name) { return }
    # Application tags are produced by the build step, not by docker pull.
    if ($name -match '^(rag_new2-|aetheris-)') { return }
    if (-not $List.Contains($name)) { [void]$List.Add($name) }
}

function Get-RemoteImagesQuiet {
    param([string[]]$ComposeArgs)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = @(& docker @ComposeArgs config --format json 2>&1)
        if ($LASTEXITCODE -ne 0) { return @() }
        $text = ($raw | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { [string]$_.Exception.Message } else { [string]$_ }
        }) -join "`n"
        $cfg = $text | ConvertFrom-Json
        return @(Get-RemoteComposeImages -Config $cfg)
    } catch {
        return @()
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Get-RequiredLibraryImages {
    # Dockerfile bases are not service images, so Compose config never lists
    # them. Without this list every backend/frontend/scanner build pulls NVIDIA,
    # Node, and Python again.
    $names = New-Object System.Collections.Generic.List[string]
    foreach ($image in @(
        'nvidia/cuda:12.6.3-runtime-ubuntu24.04',
        'nvidia/cuda:12.6.3-base-ubuntu24.04',
        'node:20-alpine',
        'nginx:1.27-alpine',
        'python:3.12-slim',
        'aquasec/trivy:0.58.1',
        'clamav/clamav:1.4',
        'certbot/certbot:v5.8.0',
        'pgvector/pgvector:pg16',
        'dpage/pgadmin4:8',
        'mailhog/mailhog:v1.0.1',
        'redis:7',
        'chainguard/minio:latest',
        'neo4j:5',
        'opensearchproject/opensearch:2',
        'zaproxy/zap-stable',
        'alpine:3',
        'registry.community.greenbone.net/community/vulnerability-tests',
        'registry.community.greenbone.net/community/notus-data',
        'registry.community.greenbone.net/community/scap-data',
        'registry.community.greenbone.net/community/cert-bund-data',
        'registry.community.greenbone.net/community/dfn-cert-data',
        'registry.community.greenbone.net/community/data-objects',
        'registry.community.greenbone.net/community/report-formats',
        'registry.community.greenbone.net/community/gpg-data',
        'registry.community.greenbone.net/community/redis-server',
        'registry.community.greenbone.net/community/pg-gvm-migrator:stable',
        'registry.community.greenbone.net/community/pg-gvm:stable',
        'registry.community.greenbone.net/community/gvmd:stable',
        'registry.community.greenbone.net/community/openvas-scanner:stable',
        'registry.community.greenbone.net/community/ospd-openvas:stable',
        'registry.community.greenbone.net/community/gvm-tools'
    )) {
        Add-UniqueImageName -List $names -Image $image
    }
    foreach ($stack in @(Get-AllStackDefinitions)) {
        $args = @($stack.Args)
        if ($stack.Name -eq 'gateway') { $args = @(Get-PublicGatewayArgs) }
        $args = @(Get-ProdDownComposeArgs -Name $stack.Name -ComposeArgs $args)
        foreach ($image in @(Get-RemoteImagesQuiet -ComposeArgs $args)) {
            Add-UniqueImageName -List $names -Image $image
        }
    }
    return @($names)
}

function Test-OptionalLibraryImage {
    param([string]$Image)
    # These feeds are large and the vulnerability stack already continues when
    # one of them cannot start. A registry miss must not block HTTPS or the APIs.
    return $Image -like 'registry.community.greenbone.net/*' -or $Image -eq 'zaproxy/zap-stable'
}

function Invoke-PullRequiredLibraries {
    $images = @(Get-RequiredLibraryImages)
    $refresh = [bool]($RefreshDependencies -or $NoCache)
    Write-Step ("Explicit maintenance prefetch: {0} library images" -f $images.Count)
    if (-not $refresh) {
        Write-Ok 'An image already stored by Docker is skipped. It is not downloaded again.'
    }
    $pulled = 0
    $skipped = 0
    foreach ($image in $images) {
        if ((-not $refresh) -and (Test-DockerImagePresent -Image $image)) {
            $skipped++
            Write-Host "    present: $image"
            continue
        }
        if (Test-OptionalLibraryImage -Image $image) {
            $code = Invoke-Docker -Title "Pull $image" -DockerArgs @('pull', $image) -PassThru
            if ($code -ne 0) {
                Write-Warn "Optional image $image was not pulled. Vulnerability scanning can be started again later."
                continue
            }
        } else {
            Invoke-Docker -Title "Pull $image" -DockerArgs @('pull', $image)
        }
        $pulled++
    }
    Write-Ok ("Library pull finished. downloaded={0} already-present={1}" -f $pulled, $skipped)
}

function Get-TorchIndexUrl {
    if ($env:TORCH_INDEX_URL -and $env:TORCH_INDEX_URL.Trim()) { return $env:TORCH_INDEX_URL.Trim() }
    $envFile = Join-Path $root '.env'
    if (Test-Path -LiteralPath $envFile -PathType Leaf) {
        foreach ($line in @(Get-Content -LiteralPath $envFile)) {
            if ($line -match '^\s*TORCH_INDEX_URL\s*=\s*(.+)\s*$') {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    return 'https://download.pytorch.org/whl/cu130'
}

function Invoke-DockerImageBuild {
    param([string]$Title, [string[]]$BuildArgs)
    $dockerArgs = @('build')
    if ($RefreshDependencies -or $NoCache) { $dockerArgs += @('--pull', '--no-cache') }
    Invoke-Docker -Title $Title -DockerArgs ($dockerArgs + $BuildArgs)
}

function Invoke-OneServiceBuild {
    param([string]$Title, [string[]]$ComposeArgs, [string]$Service, [string]$BuildId)
    $args = @($ComposeArgs) + @(Get-AppBuildArgs -BuildId $BuildId -RefreshDependencies:($RefreshDependencies -or $NoCache)) + @($Service)
    Invoke-Docker -Title $Title -DockerArgs $args
}

function Invoke-BuildProdApplications {
    param([string]$BuildId)
    $torch = Get-TorchIndexUrl
    Write-Step 'Build shared libraries once, then each application image once'
    Write-Ok 'Forensic, Android, iOS, and the vulnerability API share one backend image. It is not built four times.'

    Invoke-DockerImageBuild -Title 'Build shared backend libraries (NVIDIA CUDA, apt, PyTorch, Python requirements)' -BuildArgs @(
        '-f', 'backend/Dockerfile', '--target', 'dependencies',
        '--build-arg', "TORCH_INDEX_URL=$torch",
        '-t', 'aetheris-backend-deps', 'backend')
    Invoke-DockerImageBuild -Title 'Build shared backend application' -BuildArgs @(
        '-f', 'backend/Dockerfile',
        '--build-arg', "TORCH_INDEX_URL=$torch",
        '--build-arg', "AETHERIS_APP_BUILD_ID=$BuildId",
        '-t', 'rag_new2-api', 'backend')
    foreach ($tag in @('aetheris-mobile-android-app', 'aetheris-mobile-ios-app')) {
        Invoke-Docker -Title "Tag shared backend as $tag" -DockerArgs @('tag', 'rag_new2-api', $tag)
    }

    Invoke-DockerImageBuild -Title 'Build shared website libraries (npm ci, once)' -BuildArgs @(
        '-f', 'frontend/Dockerfile', '--target', 'dependencies',
        '-t', 'aetheris-frontend-deps', 'frontend')

    $malwareArgs = @(Get-StackComposeArgs -Root $root -Name 'mobile-malware')
    Invoke-OneServiceBuild -Title 'Build malware application (analysis libraries on the shared pip cache)' -ComposeArgs $malwareArgs -Service 'api' -BuildId $BuildId
    Invoke-OneServiceBuild -Title 'Build ClamAV image' -ComposeArgs $malwareArgs -Service 'clamav-update' -BuildId $BuildId

    $vulnArgs = @(Get-StackComposeArgs -Root $root -Name 'vuln')
    Invoke-OneServiceBuild -Title 'Build vulnerability scanner worker' -ComposeArgs $vulnArgs -Service 'worker-nessus' -BuildId $BuildId

    Invoke-OneServiceBuild -Title 'Build HTTPS gateway website' -ComposeArgs @(Get-PublicGatewayArgs) -Service 'gateway' -BuildId $BuildId
    Invoke-OneServiceBuild -Title 'Build Android website' -ComposeArgs @(Get-StackComposeArgs -Root $root -Name 'mobile-android') -Service 'frontend' -BuildId $BuildId
    Invoke-OneServiceBuild -Title 'Build iOS website' -ComposeArgs @(Get-StackComposeArgs -Root $root -Name 'mobile-ios') -Service 'frontend' -BuildId $BuildId
    Write-Ok 'Application images are built. Later Compose starts use --no-build.'
}

function Remove-ForeignComposeContainerNames {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$ComposeArgs
    )
    # A container created outside this Compose project can keep the canonical
    # service name. Compose then fails with "name already in use" and the
    # whole product stack stops. Remove only a name holder that is not this
    # project and service. Volumes and evidence stay in place.
    $project = "aetheris-$Name"
    $cfg = Get-ComposeConfig -ComposeArgs $ComposeArgs
    if (-not $cfg.services) { return }
    foreach ($property in @($cfg.services.PSObject.Properties)) {
        $service = [string]$property.Name
        $spec = $property.Value
        $expected = @()
        if ($spec.container_name) {
            $expected += [string]$spec.container_name
        } else {
            $replicas = 1
            if ($spec.deploy -and $spec.deploy.replicas) { $replicas = [int]$spec.deploy.replicas }
            if ($replicas -lt 1) { $replicas = 1 }
            for ($index = 1; $index -le $replicas; $index++) {
                $expected += "$project-$service-$index"
            }
        }
        foreach ($containerName in $expected) {
            $ids = @(& docker ps -aq --filter "name=$containerName")
            if ($LASTEXITCODE -ne 0) { throw "Cannot query containers named $containerName" }
            foreach ($id in $ids) {
                $cid = ([string]$id).Trim()
                if (-not $cid) { continue }
                $raw = & docker inspect $cid
                if ($LASTEXITCODE -ne 0) { continue }
                $item = @((($raw -join "`n") | ConvertFrom-Json))[0]
                if ([string]$item.Name -ne "/$containerName") { continue }
                $labels = $item.Config.Labels
                $ownedProject = ''
                $ownedService = ''
                if ($labels) {
                    $ownedProject = [string]$labels.'com.docker.compose.project'
                    $ownedService = [string]$labels.'com.docker.compose.service'
                }
                if ($ownedProject -eq $project -and $ownedService -eq $service) { continue }
                $owner = if ($ownedProject -or $ownedService) { "$ownedProject/$ownedService" } else { 'no Compose project' }
                throw "Container $containerName is owned by $owner, not $project/$service. Rename or stop it manually; Polaron will not remove another project's container."
            }
        }
    }
}

function Clear-StackPublishedPorts {
    param(
        [Parameter(Mandatory = $true)][string[]]$ComposeArgs,
        [string[]]$Services
    )
    # Another Docker project, or a stopped container that still reserves the
    # host port, makes `compose up` fail with "ports are not available".
    # Remove that container. The engine and WSL relay stay running.
    if (-not (Get-Command Clear-OccupiedHostPorts -ErrorAction SilentlyContinue)) {
        $script = Join-Path $root 'scripts\free-published-ports.ps1'
        if (-not (Test-Path -LiteralPath $script)) { return }
        . $script
    }
    if (-not $Services -or @($Services).Count -eq 0) {
        $cfg = Get-ComposeConfig -ComposeArgs $ComposeArgs
        if (-not $cfg -or -not $cfg.services) { return }
        $Services = @($cfg.services.PSObject.Properties.Name)
    }
    Clear-OccupiedHostPorts -Docker 'docker' -ComposeArgs $ComposeArgs -Services @($Services)
}

function Get-ConfiguredSharedOpenSearchPort {
    if ("$env:COMMON_OPENSEARCH_PORT" -match '^\d{2,5}$') { return [int]$env:COMMON_OPENSEARCH_PORT }
    $envPath = Join-Path $root '.env'
    if (Test-Path -LiteralPath $envPath -PathType Leaf) {
        foreach ($line in @(Get-Content -LiteralPath $envPath)) {
            if ($line -match '^\s*COMMON_OPENSEARCH_PORT\s*=\s*(\d+)') { return [int]$Matches[1] }
        }
    }
    return 19200
}

function Get-SharedOpenSearchContainerPort {
    # Read Docker's ACTUAL original published port, not Compose's desired config.
    $ids = @(& docker ps -aq --filter 'label=com.docker.compose.project=aetheris-common' --filter 'label=com.docker.compose.service=opensearch')
    if ($LASTEXITCODE -ne 0 -or $ids.Count -lt 1 -or -not $ids[0]) { return 0 }
    $raw = @(& docker inspect ([string]$ids[0]) --format '{{json .HostConfig.PortBindings}}')
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return 0 }
    try {
        $bindings = (($raw -join "`n") | ConvertFrom-Json)
        $hostEntries = $bindings.PSObject.Properties['9200/tcp'].Value
        if ($hostEntries -and $hostEntries.Count -gt 0 -and "$($hostEntries[0].HostPort)" -match '^\d+$') {
            return [int]$hostEntries[0].HostPort
        }
    } catch { }
    return 0
}

function Get-LegacyOpenSearchVolumeOwner {
    $volume = 'aetheris-forensic_opensearch_data'
    $owners = @(& docker ps -q --filter "volume=$volume")
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect running volume owners for $volume." }
    $legacy = @()
    foreach ($id in @($owners | Where-Object { $_ -and $_.Trim() })) {
        $raw = @(& docker inspect ([string]$id) --format '{{json .}}')
        if ($LASTEXITCODE -ne 0 -or -not $raw) { throw "Cannot inspect OpenSearch volume owner $id." }
        try { $info = (($raw -join "`n") | ConvertFrom-Json) }
        catch { throw "Malformed Docker metadata for OpenSearch volume owner $id." }
        $project = [string]$info.Config.Labels.'com.docker.compose.project'
        $service = [string]$info.Config.Labels.'com.docker.compose.service'
        if ($project -eq 'aetheris-common' -and $service -eq 'opensearch') { continue }
        if ($project -ne 'aetheris-forensic' -or $service -ne 'opensearch') {
            throw "Unexpected running container '$($info.Name)' owns $volume. Manual migration required; it will not be stopped."
        }
        $legacy += [pscustomobject]@{ Id = [string]$id; Name = ([string]$info.Name).TrimStart('/') }
    }
    if ($legacy.Count -gt 1) { throw "Multiple legacy OpenSearch owners found. Resolve manually to protect $volume." }
    if ($legacy.Count -eq 1) { return $legacy[0] }
    return $null
}

function Assert-SharedOpenSearchVolumeExclusive {
    # Guards 'aetheris-forensic_opensearch_data' from simultaneous writers.
    $owner = Get-LegacyOpenSearchVolumeOwner
    if (-not $owner) { return }
    if (-not $HandoverLegacyOpenSearch) {
        throw "Legacy OpenSearch '$($owner.Name)' still owns the shared index. During a maintenance window, with a verified index backup, rerun start_docker_local.cmd -HandoverLegacyOpenSearch (or start_docker_prod.cmd with the same flag). The handover stops ONLY the old OpenSearch and preserves the volume; never use down -v."
    }
    if ($script:LegacyOpenSearchHandover) { return }
    Write-Warn "Explicit handover requested: stopping only legacy OpenSearch '$($owner.Name)' (ID $($owner.Id))."
    $null = & docker stop --time 60 $owner.Id
    if ($LASTEXITCODE -ne 0) { throw "Could not stop legacy OpenSearch '$($owner.Name)'; refusing new writer." }
    # Keep the old container and its metadata for rollback. No rm, down, prune,
    # changes to the volume, or image pulls are performed by the handover.
    $script:LegacyOpenSearchHandover = $owner
    Write-Ok 'Legacy index owner stopped; volume retained for Common OpenSearch.'
}

function Restore-LegacyOpenSearchHandover {
    if (-not $script:LegacyOpenSearchHandover) { return }
    $owner = $script:LegacyOpenSearchHandover
    # Never start the legacy instance until the replacement has STOPPED.
    $runningCommon = @(& docker ps -q --filter 'label=com.docker.compose.project=aetheris-common' --filter 'label=com.docker.compose.service=opensearch')
    if ($LASTEXITCODE -ne 0) { throw 'Rollback blocked: cannot check whether Common OpenSearch is still running.' }
    foreach ($id in @($runningCommon | Where-Object { $_ -and $_.Trim() })) {
        $null = & docker stop --time 60 ([string]$id)
        if ($LASTEXITCODE -ne 0) { throw 'Rollback blocked: Common OpenSearch could not be stopped. Do not start the legacy writer.' }
    }
    $null = & docker start $owner.Id
    if ($LASTEXITCODE -ne 0) { throw "Could not restore legacy OpenSearch '$($owner.Name)'. The index volume is preserved; inspect Docker and restart manually." }
    $script:LegacyOpenSearchHandover = $null
    Write-Warn "Common OpenSearch was not ready; old container '$($owner.Name)' restarted (rollback)."
}

function Start-SharedCommonServices {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArgs)
    # Common Docker belongs to multiple projects: never bulk `compose up` here.
    # It recreates healthy Postgres/MinIO/pgAdmin whenever their image/config drifts.
    Ensure-ComposeExternalVolumes -ComposeArgs $ComposeArgs
    $pullFlag = Get-ComposePullFlag
    $portManager = Join-Path $root 'scripts/ensure_common_opensearch.ps1'
    $order = @('postgres','redis','minio','opensearch','mailhog','postgres-ensure','pgadmin')
    foreach ($service in $order) {
        $state = Get-ComposeServiceContainerState -ProjectName 'aetheris-common' -Service $service
        if ($state -and $state.Status -eq 'running') {
            if ($state.Health -eq 'unhealthy' -and
                -not (Wait-ComposeServiceReady -ProjectName 'aetheris-common' -Service $service -TimeoutSeconds 30)) {
                throw "Shared $service is unhealthy. Check its logs; refusing to recreate a database shared by other projects."
            }
            Write-Ok "Reuse running Common Docker: $service"
            continue
        }
        if ($service -eq 'postgres-ensure' -and $state -and $state.Status -eq 'exited' -and $state.ExitCode -eq 0) {
            Write-Ok 'Reuse completed shared database initialization.'
            continue
        }
        if ($service -in @('postgres-ensure','pgadmin')) {
            if (-not (Wait-ComposeServiceReady -ProjectName 'aetheris-common' -Service 'postgres' -TimeoutSeconds 180)) {
                throw 'Shared PostgreSQL did not become healthy. Check aetheris-common-postgres logs before starting dependent services.'
            }
        }
        if ($service -eq 'opensearch') {
            Assert-SharedOpenSearchVolumeExclusive
        }
        # An existing Created/Exited OpenSearch container may still retain the
        # OLD host port (9200) after port auto-repair. Recreate ONLY that service.
        $forceOpenSearch = ($service -eq 'opensearch' -and $state -and
            ((Get-SharedOpenSearchContainerPort) -ne (Get-ConfiguredSharedOpenSearchPort)))
        $up = @($ComposeArgs) + @('up','-d','--no-build','--pull',$pullFlag,'--no-deps')
        if ($forceOpenSearch) {
            Write-Host "    Replacing only stopped OpenSearch container to apply new host port $((Get-ConfiguredSharedOpenSearchPort))."
            $up += '--force-recreate'
        } else {
            $up += '--no-recreate'
        }
        $up += $service
        try {
        $code = Invoke-Docker -Title "Start missing Common Docker service: $service" -DockerArgs $up -PassThru
        if ($code -ne 0 -and $service -eq 'opensearch' -and
            $script:AetherisLastDockerText -match '(?i)ports are not available|bind:.*(in use|permitted)|port is already allocated') {
            # A race with Docker Desktop/another project can occur after the
            # loopback bind preflight. Choose a different port and retry once.
            if (-not (Test-Path -LiteralPath $portManager -PathType Leaf)) { throw 'Missing OpenSearch port repair script.' }
            Write-Warn 'OpenSearch host port became occupied after preflight; selecting another free port.'
            & $portManager -ProjectRoot $root -ForceAlternate -ExcludePort (Get-ConfiguredSharedOpenSearchPort)
            $retryUp = @($ComposeArgs) + @('up','-d','--no-build','--pull',$pullFlag,'--no-deps','--force-recreate','opensearch')
            $code = Invoke-Docker -Title 'Retry only OpenSearch with alternate host port' -DockerArgs $retryUp -PassThru
        }
        if ($code -ne 0) {
            throw "Cannot start Common Docker $service (exit $code). Existing databases and evidence were preserved."
        }
        if ($service -eq 'opensearch' -and $script:LegacyOpenSearchHandover) {
            if (-not (Wait-ComposeServiceReady -ProjectName 'aetheris-common' -Service 'opensearch' -TimeoutSeconds 240)) {
                throw 'New Common OpenSearch failed readiness after handover.'
            }
            Write-Ok 'Common OpenSearch healthy; legacy container retained stopped for possible manual rollback.'
            $script:LegacyOpenSearchHandover = $null
        }
        } catch {
            $reason = $_.Exception.Message
            if ($service -eq 'opensearch' -and $script:LegacyOpenSearchHandover) {
                Restore-LegacyOpenSearchHandover
            }
            throw $reason
        }
    }
    Write-Ok 'Shared Common Docker reconciled without recreating running containers.'
}

function Invoke-StackUp {
    param([string]$Name, [string[]]$ComposeArgs)
    if ($Name -eq 'common') {
        Start-SharedCommonServices -ComposeArgs $ComposeArgs
        return
    }
    # Do not kill or remove foreign containers/Windows processes to reclaim ports.
    # Docker's ordinary port conflict diagnostic is safer for shared hosts.
    Ensure-ComposeExternalVolumes -ComposeArgs $ComposeArgs
    # Compose --pull missing uses Docker's global content-addressed cache.
    # Only opt into the slower image-by-image preflight for a troublesome proxy;
    # it is not needed on every launch and can preload inactive profiles.
    if ($env:AETHERIS_SEQUENTIAL_PULL -eq '1' -and (Get-ComposePullFlag) -ne 'never') {
        Ensure-ComposeImagesPresent -ComposeArgs $ComposeArgs
    }
    $upArgs = @($ComposeArgs) + @('up', '-d', '--no-build', '--pull', (Get-ComposePullFlag))
    $code = Invoke-Docker -Title "Start $Name" -DockerArgs $upArgs -PassThru
    if ($code -eq 0) { return }
    if (Test-DockerRegistryDnsFailure $script:AetherisLastDockerText) {
        throw "Docker action 'Start $Name' failed (exit $code)."
    }
    $firstCode = $code
    # Replacing the API sends SIGTERM, which is exit 143. Compose then reports
    # "dependency api failed to start" and leaves workers Created, even when the
    # replacement API is coming up. Wait until it is healthy and start them.
    $project = "aetheris-$Name"
    $ready = Wait-ComposeServiceReady -ProjectName $project -Service 'api' -TimeoutSeconds 240
    if (-not $ready) {
        throw "Docker action 'Start $Name' failed (exit $firstCode)."
    }
    Write-Host "    $Name API is healthy after the recreate. Starting services left created."
    & docker @upArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Docker action 'Start $Name' failed (exit $LASTEXITCODE)."
    }
}

function Ensure-MobileCoreWorkersStarted {
    param([string]$Name, [string[]]$ComposeArgs)
    if ($Name -notin @('mobile-android', 'mobile-ios')) { return }

    # Direct GPU access is configured on the GPU workers. A second per-product
    # nvidia-cuda container cannot share a CUDA driver/runtime and caused repeated
    # image pulls and artificial dependency waits.
    $projectName = "aetheris-$Name"
    if (-not (Wait-ComposeServiceReady -ProjectName $projectName -Service 'api' -TimeoutSeconds 240)) {
        Write-Warn "$Name API is not yet ready; worker reconciliation will be handled by the readiness gate."
        return
    }
    foreach ($service in @('worker-build','worker-parse')) {
        $state = Get-ComposeServiceContainerState -ProjectName $projectName -Service $service
        if ($state -and $state.Status -eq 'created') {
            Write-Host "    Starting deferred $Name/$service after API became healthy..." -ForegroundColor DarkCyan
            $code = Invoke-DockerNativeSafe -DockerArgs ($ComposeArgs + @('up','-d','--no-build','--pull',(Get-ComposePullFlag),$service))
            if ($code -ne 0) {
                Write-Warn "Deferred $Name/$service start returned $code. Readiness check will explain any remaining failure."
            }
        }
    }
}

function Get-ComposeServiceContainerState {
    param([string]$ProjectName, [string]$Service)
    $ids = @(& docker ps -aq --filter "label=com.docker.compose.project=$ProjectName" --filter "label=com.docker.compose.service=$Service")
    if ($LASTEXITCODE -ne 0 -or -not $ids.Count) { return $null }
    $raw = & docker inspect ([string]$ids[0])
    if ($LASTEXITCODE -ne 0) { return $null }
    $item = (($raw -join "`n") | ConvertFrom-Json)[0]
    return [pscustomobject]@{
        Id = [string]$item.Id
        Status = [string]$item.State.Status
        Health = if ($item.State.Health) { [string]$item.State.Health.Status } else { 'none' }
        ExitCode = [int]$item.State.ExitCode
    }
}

function Wait-ComposeServiceReady {
    param(
        [string]$ProjectName,
        [string]$Service,
        [int]$TimeoutSeconds = 180,
        [switch]$AllowStartingHealth
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $state = Get-ComposeServiceContainerState -ProjectName $ProjectName -Service $Service
        if ($state) {
            if ($state.Status -eq 'running' -and $state.Health -in @('none','healthy')) { return $true }
            if ($AllowStartingHealth -and $state.Status -eq 'running' -and $state.Health -eq 'starting') { return $true }
            if ($state.Status -eq 'exited' -or $state.Status -eq 'dead') { return $false }
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Invoke-DockerNativeSafe {
    param(
        [Parameter(Mandatory = $true)][string[]]$DockerArgs,
        [switch]$Quiet
    )
    # Docker/Compose writes normal progress (Creating/Stopping/Removed) to stderr.
    # Windows PowerShell 5.1 can convert native stderr into ErrorRecord objects; with
    # $ErrorActionPreference='Stop' that normal progress can abort the whole launcher.
    # Temporarily use Continue, merge stderr into stdout, and trust the native exit code.
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if ($Quiet) {
            & docker @DockerArgs 2>&1 | Out-Null
        } else {
            & docker @DockerArgs 2>&1 | ForEach-Object { Write-Host ([string]$_) }
        }
        return [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
}

function Remove-ComposeOneShotContainer {
    param([string]$ProjectName, [string]$Service, [int]$TimeoutSeconds = 30)
    $ids = @(& docker ps -aq --filter "label=com.docker.compose.project=$ProjectName" --filter "label=com.docker.compose.service=$Service")
    if ($LASTEXITCODE -ne 0 -or -not $ids.Count) { return $true }
    foreach ($id in $ids) {
        $cid = ([string]$id).Trim()
        if (-not $cid) { continue }
        [void](Invoke-DockerNativeSafe -DockerArgs @('update','--restart=no',$cid) -Quiet)
        [void](Invoke-DockerNativeSafe -DockerArgs @('rm','-f',$cid) -Quiet)
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $left = @(& docker ps -aq --filter "label=com.docker.compose.project=$ProjectName" --filter "label=com.docker.compose.service=$Service")
        if ($LASTEXITCODE -eq 0 -and -not $left.Count) { return $true }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Invoke-VulnOneShot {
    param([string[]]$ComposeArgs, [string]$Service, [int]$Attempts = 2)
    $projectName = 'aetheris-vuln'
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        Write-Host "    Greenbone feed/init: $Service (attempt $attempt/$Attempts)" -ForegroundColor DarkCyan

        # A previous interrupted launch can leave the canonical service container in
        # created/restarting/stopping state. Remove only that disposable container; the
        # named Greenbone feed volumes are preserved. Do not use `compose rm` here because
        # its progress stream can be promoted to a terminating PowerShell error.
        if (-not (Remove-ComposeOneShotContainer -ProjectName $projectName -Service $Service -TimeoutSeconds 30)) {
            Write-Warn "Could not fully remove stale initializer container for $Service; retrying after Docker settles."
            Start-Sleep -Seconds 3
        }

        $runArgs = @($ComposeArgs) + @('--profile','gvm','run','--rm','--no-deps','--pull',(Get-ComposePullFlag),$Service)
        $exitCode = Invoke-DockerNativeSafe -DockerArgs $runArgs
        if ($exitCode -eq 0) {
            Write-Ok "Greenbone initializer '$Service' completed."
            return $true
        }

        Write-Warn "Greenbone initializer '$Service' returned exit code $exitCode."
        if ($attempt -lt $Attempts) {
            Write-Warn "$Service did not finish cleanly. Waiting for Docker memory to settle before retry."
            Start-Sleep -Seconds 12
        }
    }
    return $false
}

function Start-VulnerabilityStackPhased {
    param([string[]]$ComposeArgs)
    # Greenbone databases, scanner engines and feed volumes are dependencies,
    # not application code. Retain them during application rebuild/restart.
    # In particular the api/worker are intentionally STOPPED just before this
    # function, so they must NOT be part of the scanner reuse predicate.
    if (-not $Recreate -and -not $RefreshDependencies -and -not $NoCache) {
        $scannerReady = $true
        foreach ($service in @('redis-gvm','pg-gvm','gvmd','openvas','openvasd','ospd-openvas')) {
            $state = Get-ComposeServiceContainerState -ProjectName 'aetheris-vuln' -Service $service
            if (-not $state -or $state.Status -ne 'running' -or $state.Health -eq 'unhealthy') {
                $scannerReady = $false
                break
            }
        }
        if ($scannerReady) {
            Invoke-Docker -Title 'Restart vulnerability API from cached image' -DockerArgs (
                $ComposeArgs + @('up','-d','--no-build','--pull',(Get-ComposePullFlag),'--no-deps','api'))
            Invoke-Docker -Title 'Restart vulnerability worker from cached image' -DockerArgs (
                $ComposeArgs + @('up','-d','--no-build','--pull',(Get-ComposePullFlag),'--no-deps','worker-nessus'))
            Write-Ok 'Reused Greenbone engine and feeds; skipped initialization and scanner image downloads.'
            return
        }
    }
    # Diagnose foreign container names, never remove other projects' containers.
    Remove-ForeignComposeContainerNames -Name 'vuln' -ComposeArgs $ComposeArgs
    Write-Step 'Start vulnerability control plane (lightweight)'
    Invoke-Docker -Title 'Start vulnerability API' -DockerArgs ($ComposeArgs + @('up','-d','--no-build','--pull',(Get-ComposePullFlag),'--no-deps','api'))

    # Greenbone data images are volume loaders. Running all of them with KEEP_ALIVE caused
    # large concurrent memory spikes in Docker Desktop and exit 137. Run each loader once,
    # sequentially, then release its RAM before moving to the next feed.
    Write-Step 'Initialize Greenbone feeds sequentially'
    $oneShots = @(
        'gpg-data',
        'vulnerability-tests',
        'notus-data',
        'scap-data',
        'cert-bund-data',
        'dfn-cert-data',
        'data-objects',
        'report-formats',
        'pg-gvm-migrator',
        'configure-openvas'
    )
    foreach ($service in $oneShots) {
        if (-not (Invoke-VulnOneShot -ComposeArgs $ComposeArgs -Service $service -Attempts 2)) {
            Write-Warn "Greenbone initializer '$service' did not finish successfully. The platform will continue; vulnerability scanning will report feed warming/unavailable until repaired."
            break
        }
    }

    Write-Step 'Start Greenbone runtime in dependency order'
    $gvmArgs = @($ComposeArgs) + @('--profile','gvm')
    foreach ($service in @('redis-gvm','pg-gvm')) {
        & docker @($gvmArgs + @('up','-d','--no-build','--pull',(Get-ComposePullFlag),'--no-deps',$service))
        if ($LASTEXITCODE -ne 0) { Write-Warn "Could not start $service; scanner engine will remain degraded."; continue }
        if (-not (Wait-ComposeServiceReady -ProjectName 'aetheris-vuln' -Service $service -TimeoutSeconds 180 -AllowStartingHealth)) {
            Write-Warn "$service is not ready yet; scanner engine will continue warming in the background."
        }
    }
    foreach ($service in @('openvas','openvasd','ospd-openvas','gvmd','gvm-tools')) {
        & docker @($gvmArgs + @('up','-d','--no-build','--pull',(Get-ComposePullFlag),'--no-deps',$service))
        if ($LASTEXITCODE -ne 0) { Write-Warn "Could not start $service; scanner engine will remain degraded."; continue }
        if (-not (Wait-ComposeServiceReady -ProjectName 'aetheris-vuln' -Service $service -TimeoutSeconds 90 -AllowStartingHealth)) {
            Write-Warn "$service is not ready yet; scanner engine will continue warming in the background."
        }
    }

    # Start the application worker only after the scanner sockets have been given a chance to
    # appear. It can still run while Greenbone finishes feed import and will expose readiness.
    & docker @($ComposeArgs + @('up','-d','--no-build','--pull',(Get-ComposePullFlag),'--no-deps','worker-nessus'))
    if ($LASTEXITCODE -ne 0) { throw 'Vulnerability worker could not be started.' }

    Write-Step 'Start optional vulnerability sidecars'
    $sidecarArgs = @($ComposeArgs) + @('--profile','vuln-scanners')
    foreach ($service in @('trivy','zap')) {
        & docker @($sidecarArgs + @('up','-d','--no-build','--pull',(Get-ComposePullFlag),'--no-deps',$service))
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "$service did not start. Core IP/OpenVAS scanning remains available; this sidecar can be restarted later."
        }
    }
}


function Get-CommonRedisState {
    $ids = @(& docker ps -aq --filter "label=com.docker.compose.project=aetheris-common" --filter "label=com.docker.compose.service=redis")
    if ($LASTEXITCODE -ne 0 -or -not $ids.Count) { return $null }
    $id = [string]$ids[0]
    $raw = & docker inspect $id
    if ($LASTEXITCODE -ne 0) { return $null }
    $item = (($raw -join "`n") | ConvertFrom-Json)[0]
    $health = if ($item.State.Health) { [string]$item.State.Health.Status } else { 'none' }
    $volume = @($item.Mounts | Where-Object { $_.Destination -eq '/data' -and $_.Type -eq 'volume' } | Select-Object -First 1)
    return [pscustomobject]@{
        Id = $id
        Name = [string]$item.Name
        Status = [string]$item.State.Status
        Health = $health
        ExitCode = [int]$item.State.ExitCode
        Volume = if ($volume) { [string]$volume.Name } else { '' }
    }
}

function Wait-CommonRedisHealthy {
    param([int]$TimeoutSeconds = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $state = Get-CommonRedisState
        if ($state -and $state.Status -eq 'running' -and $state.Health -in @('healthy','none')) { return $true }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Backup-RedisVolume {
    param([Parameter(Mandatory = $true)][string]$VolumeName)
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $backupVolume = "${VolumeName}_backup_${stamp}"
    Write-Warn "Backing up Redis volume '$VolumeName' to '$backupVolume' before repair."
    & docker volume create $backupVolume 1>$null
    if ($LASTEXITCODE -ne 0) { throw "Could not create Redis backup volume $backupVolume." }
    & docker run --rm -v "${VolumeName}:/from:ro" -v "${backupVolume}:/to" alpine:3 sh -c 'cp -a /from/. /to/'
    if ($LASTEXITCODE -ne 0) { throw "Could not back up Redis volume $VolumeName." }
    return $backupVolume
}

function Remove-CommonRedisContainerForRepair {
    param([string]$ContainerId = '')
    if (-not $ContainerId) {
        $state = Get-CommonRedisState
        if (-not $state) { return }
        $ContainerId = [string]$state.Id
    }

    Write-Warn 'Detaching crash-looping Redis container before persistence repair.'
    $oldEap = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 can surface native stderr as an ErrorRecord while
        # ErrorActionPreference=Stop.  Crash-loop cleanup is intentionally best-effort;
        # verify removal explicitly below instead of aborting on Docker's progress text.
        $ErrorActionPreference = 'Continue'
        & docker update --restart=no $ContainerId 1>$null 2>$null
        & docker rm -f $ContainerId 1>$null 2>$null
    } finally {
        $ErrorActionPreference = $oldEap
    }

    $removeDeadline = (Get-Date).AddSeconds(20)
    do {
        $stillThere = @(& docker ps -aq --filter "id=$ContainerId" 2>$null)
        if (-not $stillThere.Count) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $removeDeadline)
    throw "Redis container could not be removed from Docker's stopping state. Run: docker rm -f $ContainerId"
}

function Repair-CommonRedis {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArgs)
    $state = Get-CommonRedisState
    if (-not $state) { throw 'Shared Redis container was not created.' }

    Write-Warn "Redis is not healthy: status=$($state.Status) health=$($state.Health) exit=$($state.ExitCode)."
    Write-Host '---- Redis logs (last 120 lines) ----' -ForegroundColor DarkYellow
    & docker logs --tail 120 $state.Id 2>&1 | ForEach-Object { Write-Host $_ }
    Write-Host '------------------------------------' -ForegroundColor DarkYellow

    if (-not $state.Volume) {
        throw 'Redis /data is not backed by a Docker volume, so automatic safe repair is disabled.'
    }

    $backupVolume = Backup-RedisVolume -VolumeName $state.Volume
    Write-Warn "Redis recovery backup retained as Docker volume: $backupVolume"

    # A Redis container in a restart loop can remain in Docker's "Stopping" state.
    # Force-remove only the container; the named /data volume is retained.
    Remove-CommonRedisContainerForRepair -ContainerId $state.Id

    # First pass: normalize ownership and repair Redis 7 multi-part or legacy AOF data.
    $repairScript = @'
set -eu
chown -R redis:redis /data || true
if [ -f /data/appendonlydir/appendonly.aof.manifest ]; then
  echo "Repairing Redis multi-part AOF manifest..."
  yes y | redis-check-aof --fix /data/appendonlydir/appendonly.aof.manifest || true
elif [ -f /data/appendonly.aof ]; then
  echo "Repairing legacy Redis AOF..."
  yes y | redis-check-aof --fix /data/appendonly.aof || true
fi
chown -R redis:redis /data || true
'@
    & docker run --rm --user root -v "$($state.Volume):/data" redis:7 sh -c $repairScript
    if ($LASTEXITCODE -ne 0) { Write-Warn 'Redis persistence repair command returned a warning; attempting restart before fallback reset.' }

    $redisUpArgs = @($ComposeArgs) + @('up', '-d', '--no-build', '--pull', (Get-ComposePullFlag), '--force-recreate', 'redis')
    & docker @redisUpArgs
    if ($LASTEXITCODE -eq 0 -and (Wait-CommonRedisHealthy -TimeoutSeconds 35)) {
        Write-Ok 'Shared Redis recovered successfully without clearing persistence.'
        return
    }

    # Last resort: Redis is queue/cache infrastructure, not the forensic evidence store.
    # The first restart may itself be crash-looping on persistence. Remove that container
    # before moving files in the named volume so Redis cannot write concurrently.
    $failedRestart = Get-CommonRedisState
    if ($failedRestart) { Remove-CommonRedisContainerForRepair -ContainerId $failedRestart.Id }

    # Preserve the complete failed volume above, then quarantine persistence files so the
    # service can start cleanly. The backup can be examined/restored later if required.
    Write-Warn 'Redis still cannot start. Quarantining its persistence files and starting a clean transient data set.'
    $quarantineScript = @'
set -eu
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "/data/recovery-$stamp"
if [ -d /data/appendonlydir ]; then mv /data/appendonlydir "/data/recovery-$stamp/appendonlydir"; fi
if [ -f /data/appendonly.aof ]; then mv /data/appendonly.aof "/data/recovery-$stamp/appendonly.aof"; fi
if [ -f /data/dump.rdb ]; then mv /data/dump.rdb "/data/recovery-$stamp/dump.rdb"; fi
chown -R redis:redis /data || true
'@
    & docker run --rm --user root -v "$($state.Volume):/data" redis:7 sh -c $quarantineScript
    if ($LASTEXITCODE -ne 0) { throw 'Redis persistence quarantine failed; see the Redis logs above.' }

    $redisUpArgs = @($ComposeArgs) + @('up', '-d', '--no-build', '--pull', (Get-ComposePullFlag), '--force-recreate', 'redis')
    & docker @redisUpArgs
    if ($LASTEXITCODE -ne 0 -or -not (Wait-CommonRedisHealthy -TimeoutSeconds 35)) {
        $final = Get-CommonRedisState
        if ($final) { & docker logs --tail 120 $final.Id 2>&1 | ForEach-Object { Write-Host $_ } }
        throw "Shared Redis could not be recovered automatically. Backup volume retained: $backupVolume"
    }
    Write-Ok "Shared Redis started with clean persistence. Previous Redis data is preserved in $backupVolume."
}

function Ensure-CommonRedisHealthy {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArgs)
    if (Wait-CommonRedisHealthy -TimeoutSeconds 25) {
        Write-Ok 'Shared Redis is healthy.'
        return
    }
    Repair-CommonRedis -ComposeArgs $ComposeArgs
}

function Get-PublicGatewayArgs {
    return @('compose', '--project-directory', $root, '--project-name', 'aetheris-gateway',
        '-f', 'services/gateway/docker-compose.yml',
        '-f', 'deployment/windows-ip-https/docker-compose.gateway-public-https.yml')
}

function Get-LocalGatewayArgs {
    return @('compose', '--project-directory', $root, '--project-name', 'aetheris-gateway',
        '-f', 'services/gateway/docker-compose.yml',
        '-f', 'deployment/windows-ip-https/docker-compose.gateway-local.yml')
}

function Start-AcmeBootstrap {
    param([string[]]$ComposeArgs)
    Invoke-Docker -Title 'Start temporary HTTP-01 certificate bootstrap on TCP 80' -DockerArgs (
        $ComposeArgs + @('--profile', 'acme-bootstrap', 'up', '-d', '--no-build', '--pull', (Get-ComposePullFlag), 'acme-bootstrap'))
}

function Wait-AcmeChallengeRoute {
    param([string[]]$ComposeArgs)
    # Confirm this host answers the challenge before Certbot contacts Let's Encrypt.
    $bytes = New-Object byte[] 16
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $token = ([System.BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
    try {
        Invoke-CertificateTool -ComposeArgs $ComposeArgs -ToolArgs @('challenge', '--token', $token) | Out-Null
        foreach ($attempt in 1..15) {
            if (Test-AcmeRoute -Token $token) { return }
            Start-Sleep -Seconds 1
        }
        throw "HTTP-01 challenge file was not served on http://127.0.0.1/.well-known/acme-challenge/. TCP 80 must answer on this host, and the router must forward public TCP 80, before Let's Encrypt can renew the certificate."
    } finally {
        try { Invoke-CertificateTool -ComposeArgs $ComposeArgs -ToolArgs @('challenge', '--token', $token, '--remove') | Out-Null } catch { }
    }
}

function Stop-AcmeBootstrap {
    param([string[]]$ComposeArgs)
    try {
        Invoke-Docker -Title 'Remove temporary HTTP-01 certificate bootstrap' -DockerArgs (
            $ComposeArgs + @('--profile', 'acme-bootstrap', 'rm', '-sf', 'acme-bootstrap'))
    } catch { Write-Warn $_.Exception.Message }
}

function Get-PublicCertificateIdentities {
    # The certificates the HTTPS gateway serves: the IP always, the domain when configured.
    param([string]$IP, [string]$DNS)
    $rows = @([pscustomobject]@{ Name = "aetheris-ip-$IP"; Identity = $IP; IsIP = $true })
    if ($DNS) { $rows += [pscustomobject]@{ Name = "aetheris-domain-$DNS"; Identity = $DNS; IsIP = $false } }
    return $rows
}

function Test-PublicCertificatesCurrent {
    # True when every served certificate is a production ACME lineage that matches its identity
    # and has more than 36 hours left, so this launch needs no HTTP-01 challenge.
    param([string[]]$ComposeArgs, $Identities)
    foreach ($entry in @($Identities)) {
        $check = Invoke-CertificateTool -ComposeArgs $ComposeArgs -ToolArgs @('inspect', '--name', $entry.Name, '--identity', $entry.Identity)
        if (-not $check.valid -or -not $check.managed) { return $false }
        $expiry = [DateTime]::MinValue
        if (-not [DateTime]::TryParse([string]$check.expires_utc, [ref]$expiry) -or
            $expiry.ToUniversalTime() -le [DateTime]::UtcNow.AddHours(36)) { return $false }
    }
    return $true
}

function Set-RenewerCertificateNames {
    # certbot-renewer renews only these certificates (script_docker/renew-certificates.sh).
    param($Identities)
    $env:AETHERIS_CERT_NAMES = (@($Identities | ForEach-Object { $_.Name }) -join ' ')
}

function Test-DomainResolvesTo {
    # Advisory only: Let's Encrypt checks public DNS, which can differ from this PC's resolver.
    # Returns $true / $false, or $null when the name does not resolve here at all.
    param([string]$Domain, [string]$IP)
    try { $addresses = @([System.Net.Dns]::GetHostAddresses($Domain) | ForEach-Object { $_.ToString() }) }
    catch { return $null }
    return ($addresses -contains $IP)
}

function Ensure-PublicHttpsEdge {
    param([string]$IP, [string]$DNS, [string]$Email)

    $composeArgs = @(Get-PublicGatewayArgs)
    $nginxPath = Join-Path $root 'deployment/windows-ip-https/nginx.gateway.https.conf'
    $script:AetherisServedDomain = $DNS
    $wanted = @(Get-PublicCertificateIdentities -IP $IP -DNS $DNS)

    $current = $false
    try { $current = [bool](Test-PublicCertificatesCurrent -ComposeArgs $composeArgs -Identities $wanted) }
    catch { Write-Warn "Stored HTTPS certificates need verification or renewal: $($_.Exception.Message)" }

    # Fast path: certificates current, same IP/domain, same gateway image. Start the stopped gateway
    # again without TCP 80 downtime or an ACME challenge, and restart the renewer: it shares the
    # gateway's PID namespace (to reload nginx), so it stopped together with the gateway.
    if ($current -and -not $Recreate -and -not $script:AetherisAppImagesChanged) {
        $expected = Get-PublicNginxConfig -PublicIP $IP -Domain $DNS
        $onDisk = if (Test-Path -LiteralPath $nginxPath -PathType Leaf) { [System.IO.File]::ReadAllText($nginxPath) } else { '' }
        if (($onDisk -replace "`r", '') -eq ($expected -replace "`r", '')) {
            try {
                Set-RenewerCertificateNames -Identities $wanted
                Invoke-Docker -Title 'Start cached HTTPS gateway (certificates current; no ACME challenge)' -DockerArgs (
                    $composeArgs + @('up', '-d', '--no-build', '--pull', (Get-ComposePullFlag), '--no-deps', '--wait', '--wait-timeout', '180', 'gateway'))
                Invoke-Docker -Title 'Restart automatic certificate renewal' -DockerArgs (
                    $composeArgs + @('up', '-d', '--no-build', '--pull', (Get-ComposePullFlag), '--no-deps', '--force-recreate', 'certbot-renewer'))
                Assert-PublicGatewayBindings -ComposeArgs $composeArgs
                Write-Ok 'HTTPS certificates are current; gateway and renewal restarted without an ACME challenge.'
                return $composeArgs
            } catch {
                Write-Warn "Cached HTTPS gateway did not start cleanly; recreating it: $($_.Exception.Message)"
            }
        }
    }
    Assert-PublicPortOwners
    Enable-PublicFirewall

    $allowStored = $false
    if (-not $current) {
        if ($DNS) {
            $resolves = Test-DomainResolvesTo -Domain $DNS -IP $IP
            if ($null -eq $resolves) {
                Write-Warn "$DNS does not resolve from this PC. Its public DNS A record must be $IP before Let's Encrypt can issue its certificate."
            } elseif (-not $resolves) {
                Write-Warn "$DNS does not resolve to $IP from this PC. Let's Encrypt uses the public DNS A record, which must be $IP."
            }
        }
        # Stop-PolaronStacks has already stopped the HTTPS gateway. A certificate that is due still
        # needs something answering TCP 80; otherwise Certbot reports "Some challenges have failed".
        try { Invoke-Docker -Title 'Pause owned HTTPS gateway so the certificate challenge can bind TCP 80' -DockerArgs ($composeArgs + @('rm', '-sf', 'gateway', 'certbot-renewer')) } catch { }
        Start-AcmeBootstrap -ComposeArgs $composeArgs
        $failed = @{}
        try {
            $routeReady = $true
            try { Wait-AcmeChallengeRoute -ComposeArgs $composeArgs }
            catch { $routeReady = $false; Write-Warn $_.Exception.Message }
            foreach ($entry in $wanted) {
                if (-not $routeReady) { $failed[$entry.Name] = $true; continue }
                try {
                    Ensure-PublicCertificate -ComposeArgs $composeArgs -Identity $entry.Identity -CertName $entry.Name -Email $Email -IsIP:$entry.IsIP
                } catch {
                    # Let's Encrypt often cannot fetch the HTTP-01 file on this link (no public route to
                    # TCP 80). One failed identity must not block the other or every product stack.
                    $failed[$entry.Name] = $true
                    Write-Warn $_.Exception.Message
                }
            }
        } finally {
            Stop-AcmeBootstrap -ComposeArgs $composeArgs
        }
        foreach ($entry in $wanted) {
            if (-not $failed[$entry.Name]) { continue }
            $check = Invoke-CertificateTool -ComposeArgs $composeArgs -ToolArgs @('inspect', '--name', $entry.Name, '--identity', $entry.Identity)
            if ($check.managed) {
                $allowStored = $true
                Write-Warn "Let's Encrypt could not validate $($entry.Identity) over public TCP 80. Startup continues with the stored certificate (expires $($check.expires_utc)). Forward public TCP 80 and 443 to this PC, then start again to renew it."
            } elseif ($entry.IsIP) {
                throw "Certificate issuance for $IP failed and no certificate is stored. Public TCP 80 must reach this host before the first certificate can be issued."
            } else {
                $script:AetherisServedDomain = ''
                Write-Warn "No certificate for $DNS yet, so HTTPS starts on https://$IP/ only. Point the DNS A record of $DNS to $IP, keep public TCP 80 forwarded to this PC, then start again."
            }
        }
    }

    $served = @(Get-PublicCertificateIdentities -IP $IP -DNS $script:AetherisServedDomain)
    Set-RenewerCertificateNames -Identities $served
    $nginxText = Get-PublicNginxConfig -PublicIP $IP -Domain $script:AetherisServedDomain
    [System.IO.File]::WriteAllText($nginxPath, $nginxText, [System.Text.UTF8Encoding]::new($false))

    Start-PublicHttpsGateway -ComposeArgs $composeArgs -PublicIP $IP -Domain $script:AetherisServedDomain -AllowStoredCertificate:$allowStored
    return $composeArgs
}

if ($env:AETHERIS_START_DOCKER_FUNCTIONS_ONLY -eq '1') { return }

$exitCode = 0
$isPublic = ($Profile -ne 'local')
$publicGatewayStarted = $false

Push-Location $root
try {
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor DarkCyan
    Write-Host " Aetheris / Polaron startup - profile: $Profile" -ForegroundColor Cyan
    Write-Host " Root: $root"
    Write-Host '============================================================' -ForegroundColor DarkCyan

    Write-Step 'Initialize first-launch configuration'
    Initialize-DeploymentFiles -Root $root -Profile $Profile
    # Auto-repair only the known invalid pgAdmin email template from old releases.
    # Must run before Compose config validation, including -PrepareOnly / -ValidateOnly.
    $pgadminEnvRepair = Join-Path $root 'scripts/ensure_pgadmin_email.ps1'
    if (-not (Test-Path -LiteralPath $pgadminEnvRepair -PathType Leaf)) {
        throw "Missing pgAdmin configuration preflight: $pgadminEnvRepair"
    }
    & $pgadminEnvRepair -ProjectRoot $root
    # Upgrade only known default OCR endpoint settings; preserve custom endpoints.
    $suryaEnvAlign = Join-Path $root 'scripts/align_surya_env.ps1'
    if ((-not $ValidateOnly) -and (-not $PrepareOnly) -and (Test-Path -LiteralPath $suryaEnvAlign -PathType Leaf)) {
        & $suryaEnvAlign -ProjectRoot $root
    }
    Write-Ok '.env and host settings are present; custom values were preserved.' 

    if ($PrepareOnly) {
        Write-Ok 'PrepareOnly completed. No Docker operation was performed.'
        exit 0
    }

    Assert-DockerReady
    Assert-StartupAssets -Public $isPublic

    # Refresh BEFORE Compose preflight; a stale removable G:/ bind from the last
    # deployment must never fail validation or reach the Docker daemon.
    $driveGenerator = Join-Path $root 'scripts/generate-drive-mounts.ps1'
    if (Test-Path -LiteralPath $driveGenerator -PathType Leaf) {
        Write-Step 'Refresh Windows drive/evidence mount configuration'
        Invoke-DriveMountRefresh -Generator $driveGenerator -Public $isPublic
    } else {
        throw "Missing drive generator: $driveGenerator"
    }
    Assert-AllComposeFiles -CreateRuntimeDirectories:(-not $ValidateOnly)

    if ($ValidateOnly) {
        Write-Ok 'ValidateOnly completed. Compose files and deployment paths are valid; no containers were started.'
        exit 0
    }

    # Resolve OpenSearch's Windows host-port conflict before stopping any active
    # Polaron containers. The container still listens at opensearch:9200.
    $opensearchPortGuard = Join-Path $root 'scripts/ensure_common_opensearch.ps1'
    if (-not (Test-Path -LiteralPath $opensearchPortGuard -PathType Leaf)) {
        throw "Missing shared OpenSearch port preflight: $opensearchPortGuard"
    }
    Write-Step 'Check shared OpenSearch host port without disturbing other applications'
    & $opensearchPortGuard -ProjectRoot $root

    $settings = Get-DeploymentSettings -Root $root -Profile $Profile -PublicIP $PublicIP -Domain $Domain -AcmeEmail $AcmeEmail
    if ($isPublic -and -not $settings.AcmeEmail) {
        if (-not [Environment]::UserInteractive) {
            throw 'Set PUBLIC_HTTPS_EMAIL in .env (prod), acmeEmail in script_docker/start_docker.settings.json, or CERTBOT_EMAIL before public startup.'
        }
        $settings.AcmeEmail = Read-Host 'Let''s Encrypt email address'
        if (-not $settings.AcmeEmail) { throw 'An email address is required for trusted public HTTPS.' }
    }
    if ($isPublic) {
        Save-DeploymentSettings -Settings $settings -Profile $Profile
        $domainNote = if ($settings.Domain) { " and https://$($settings.Domain)/" } else { ' (no domain set: IP only)' }
        Write-Ok ("HTTPS address from {0}: https://{1}/{2}" -f $settings.IPSource, $settings.PublicIP, $domainNote)
        if ($Profile -eq 'prod' -and -not $settings.IPFromDotEnv) {
            Write-Warn "Move this address into .env: PUBLIC_HTTPS_IP=$($settings.PublicIP) and PUBLIC_HTTPS_DOMAIN=$($settings.Domain) (empty = IP only)."
        }
    }

    # Provision the host OCR service BEFORE stopping active Polaron containers.
    # If installation/model download/GPU setup fails, existing case workers stay up.
    Write-Step 'Ensure host SuryaOCR inference is installed, cached and ready'
    $suryaManager = Join-Path $root 'scripts/ensure_host_surya.ps1'
    if (-not (Test-Path -LiteralPath $suryaManager -PathType Leaf)) {
        throw "Missing Surya host manager: $suryaManager"
    }
    # Existing installs using a custom external vLLM endpoint may opt out of
    # native provisioning with SURYA_HOST_MODE=external.
    $suryaHostMode = if ($env:SURYA_HOST_MODE) { $env:SURYA_HOST_MODE.Trim().ToLowerInvariant() } else { 'native' }
    if ($suryaHostMode -eq 'native') {
        & $suryaManager -ProjectRoot $root
        if ($LASTEXITCODE -ne 0) { throw 'Host SuryaOCR provisioning failed; preserving running forensic services.' }
    } elseif ($suryaHostMode -eq 'external') {
        Write-Ok 'External SuryaOCR selected; keeping the administrator-managed endpoint.'
    } elseif ($suryaHostMode -eq 'disabled') {
        Write-Warn 'SuryaOCR disabled; OCR-dependent work cannot be considered fully complete.'
    } else {
        throw 'SURYA_HOST_MODE must be native, external, or disabled.'
    }

    # Normal launch ALWAYS runs the cached source build and restarts Polaron's
    # application containers. The expensive apt/pip/npm/CUDA dependency layers
    # are reused by BuildKit; unchanged sources reuse their compiled layers too.
    # New dependency tags are downloaded ONLY when absent, BEFORE application
    # containers are stopped, so a first-time registry error is not disruptive.
    $env:DOCKER_BUILDKIT = '1'
    $env:COMPOSE_DOCKER_CLI_BUILD = '1'
    if ($RefreshDependencies -or $NoCache) {
        $buildId = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
        Write-Step 'Explicit dependency refresh requested; cache will be bypassed'
    } else {
        $buildId = Get-StableAppBuildId -Root $root
        Write-Step "Prepare cached source compile (build id $buildId)"
    }
    $script:AetherisAppImagesChanged = [bool](($RefreshDependencies -or $NoCache) -or
        (-not (Test-ApplicationImagesCurrent -BuildId $buildId)))

    # Host OCR, drive mounts, image inventory and required vendor downloads are
    # checked before stopping current case workers. Common Docker stays up.
    Ensure-StartupImageCache
    if ($env:AETHERIS_PREFETCH_ALL_IMAGES -eq '1' -and ($RefreshDependencies -or $NoCache)) {
        Invoke-PullRequiredLibraries
    }
    # Resolve/start Common Docker BEFORE stopping forensic case workers. A bad
    # port, missing dependency or unhealthy shared database must never take an
    # otherwise-running Polaron case offline.
    Write-Step 'Reconcile shared Common Docker safely before application shutdown'
    Ensure-SharedDockerNetwork
    $commonPreflightArgs = @(Get-StackComposeArgs -Root $root -Name 'common')
    Ensure-CachedMinioImageAlias
    Start-SharedCommonServices -ComposeArgs $commonPreflightArgs
    if (-not (Wait-ComposeServiceReady -ProjectName 'aetheris-common' -Service 'postgres' -TimeoutSeconds 180)) {
        throw 'Common Docker PostgreSQL is not healthy; Polaron applications were not stopped.'
    }
    if (-not (Wait-ComposeServiceReady -ProjectName 'aetheris-common' -Service 'opensearch' -TimeoutSeconds 240)) {
        throw 'Common Docker OpenSearch is not healthy. Check shared OpenSearch logs and port mapping; Polaron applications were not stopped.'
    }
    Wait-MinIOHttpReady
    Stop-PolaronStacks
    if ($Recreate) {
        # Explicit maintenance: remove only Polaron-owned containers. Volumes
        # and shared Common Docker services remain untouched.
        if ($Profile -eq 'prod') { Invoke-DownPolaronProjects }
    }

    # Always invoke the build pipeline so code and lockfile changes are found.
    # Dockerfile dependency stages use BuildKit caches; they never install the
    # same packages again unless manifests, base images or cache have changed.
    Invoke-BuildProdApplications -BuildId $buildId
    Write-Ok 'Source build completed using cached dependency layers; restarting Polaron applications.'

    # Only missing third-party images may be fetched by Compose. Already-present
    # Docker images are never refreshed during normal application restarts.
    $script:AetherisImagePullPolicy = 'missing'

    Remove-LegacyGpuSidecars

    # V55: the HTTPS gateway routes to product APIs directly over the shared Docker network.
    # Create that external network before starting the gateway, including on a first launch.
    Ensure-SharedDockerNetwork

    # Public edge intentionally comes before database/application products. This makes the
    # TLS endpoint deterministic and keeps certificate validation independent of product startup.
    if ($isPublic) {
        Write-Step 'Validate trusted public certificates and start HTTPS gateway first'
        $null = Ensure-PublicHttpsEdge -IP $settings.PublicIP -DNS $settings.Domain -Email $settings.AcmeEmail
        $publicGatewayStarted = $true
        # Emailed activation/reset/login links must open the public HTTPS site, not localhost.
        $publicHost = if ($script:AetherisServedDomain) { $script:AetherisServedDomain } else { $settings.PublicIP }
        $env:APP_BASE_URL = Get-PublicAppBaseUrl -Root $root -HttpsUrl "https://$publicHost"
        Write-Ok "Emailed links (APP_BASE_URL) use $($env:APP_BASE_URL)"
    }

    Write-Step "Start shared infrastructure and verify Redis before product stacks"
    $commonArgs = @(Get-StackComposeArgs -Root $root -Name 'common')
    Ensure-CachedMinioImageAlias
    Invoke-StackUp -Name 'common' -ComposeArgs $commonArgs
    Wait-MinIOHttpReady
    Ensure-CommonRedisHealthy -ComposeArgs $commonArgs

    Write-Step "Start product stacks"
    # mobile-malware reads finished Android/iOS jobs read-only, so it starts after them.
    foreach ($name in @('forensic', 'mobile-android', 'mobile-ios', 'mobile-malware')) {
        $args = @(Get-StackComposeArgs -Root $root -Name $name)
        Invoke-StackUp -Name $name -ComposeArgs $args
        Ensure-MobileCoreWorkersStarted -Name $name -ComposeArgs $args
    }
    $vulnArgs = @(Get-StackComposeArgs -Root $root -Name 'vuln')
    Start-VulnerabilityStackPhased -ComposeArgs $vulnArgs

    if (-not $isPublic) {
        Write-Step 'Start local loopback gateway'
        $localGateway = @(Get-LocalGatewayArgs)
        Invoke-StackUp -Name 'gateway' -ComposeArgs $localGateway
    }

    # Confirm the OCR endpoint is reachable from the forensic Docker network,
    # not only from Windows. A running host listener alone is not sufficient.
    if ($env:SURYA_HOST_MODE -ne 'disabled') {
        Write-Step 'Verify Surya inference is reachable from forensic API container'
        $checkCode = "import json,os,urllib.request;u=os.environ.get('SURYA_INFERENCE_URL','http://host.docker.internal:8000/v1').rstrip('/');j=json.load(urllib.request.urlopen(u+'/models',timeout=7));assert any('surya' in str(x.get('id','')).lower() for x in j.get('data',[])), 'Surya model not found';print('Surya OCR is reachable from Docker')"
        $readyCheck = $false
        for ($attempt = 1; $attempt -le 12; $attempt++) {
            $apiContainer = @(& docker ps --filter 'label=com.docker.compose.project=aetheris-forensic' --filter 'label=com.docker.compose.service=api' -q | Select-Object -First 1)
            if ($apiContainer.Count -gt 0 -and $apiContainer[0]) {
                & docker exec $apiContainer[0] python -c $checkCode
                if ($LASTEXITCODE -eq 0) { $readyCheck = $true; break }
            }
            Start-Sleep -Seconds 5
        }
        if (-not $readyCheck) { throw 'SuryaOCR host endpoint is not accessible from forensic Docker API. Check Windows firewall, port, container host.docker.internal and model readiness.' }
    }

    # Ollama is a Windows host application, NOT a Docker Compose service.
    # Validate its API from the forensic network independently of retired
    # aetheris-forensic-ollama/-ollama-init container history.
    Write-Step 'Verify Windows host Ollama is reachable from forensic API container'
    $ollamaProbe = "import json,os,urllib.request;u=os.environ.get('OLLAMA_BASE_URL','http://host.docker.internal:11434').rstrip('/');j=json.load(urllib.request.urlopen(u+'/api/tags',timeout=8));assert isinstance(j.get('models'),list),'Ollama models field absent';print('Windows host Ollama is reachable from Docker; models='+str(len(j['models'])))"
    $ollamaReady = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $apiContainer = @(& docker ps -q --filter 'label=com.docker.compose.project=aetheris-forensic' --filter 'label=com.docker.compose.service=api' | Select-Object -First 1)
        if ($apiContainer.Count -gt 0 -and $apiContainer[0]) {
            & docker exec $apiContainer[0] python -c $ollamaProbe
            if ($LASTEXITCODE -eq 0) { $ollamaReady = $true; break }
        }
        if ($attempt -lt 3) { Start-Sleep -Seconds 3 }
    }
    if (-not $ollamaReady) {
        throw 'Windows host Ollama is unreachable from forensic API. Start the Windows Ollama application and verify http://127.0.0.1:11434/api/tags; Docker containers must use http://host.docker.internal:11434. No Docker Ollama is required.'
    }

    Write-Step 'Check container readiness' 
    $deadline = (Get-Date).AddMinutes(8)
    do {
        $report = Get-StackContainerReport
        if ($report.Failed.Count -gt 0) {
            throw "One or more containers failed: $($report.Failed -join '; ')"
        }
        if ($report.Missing.Count -eq 0 -and $report.Pending -eq 0) { break }
        Write-Host "    running=$($report.Running) pending=$($report.Pending) missing=$($report.Missing -join ',')"
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    $report = Get-StackContainerReport
    if ($report.Failed.Count -gt 0 -or $report.Missing.Count -gt 0 -or $report.Pending -gt 0) {
        throw "Startup readiness timeout. failed=$($report.Failed -join '; ') missing=$($report.Missing -join ',') pending=$($report.Pending)"
    }
    if ($report.OptionalFailed.Count -gt 0) {
        Write-Warn "Optional vulnerability engines are still warming/degraded: $($report.OptionalFailed -join '; ')"
    }
    Write-Ok ("Required containers are up ({0} running)." -f $report.Running)

    if ($isPublic) {
        $renewer = Get-ComposeServiceContainerState -ProjectName 'aetheris-gateway' -Service 'certbot-renewer'
        if (-not $renewer -or $renewer.Status -ne 'running') {
            throw 'Certificate renewal container certbot-renewer is not running. HTTPS would not renew the IP certificate.'
        }
        Write-Ok 'Certificate renewal is running. It checks every 6 hours and reloads nginx only after a successful renew.'
    }

    Assert-RuntimeHealth
    Ensure-WorkstationHelper

    Write-Host ''
    if ($isPublic) {
        Write-Host "READY: https://$($settings.PublicIP)/" -ForegroundColor Green
        if ($script:AetherisServedDomain) {
            Write-Host "READY: https://$($script:AetherisServedDomain)/" -ForegroundColor Green
        } elseif ($settings.Domain) {
            Write-Host "NOT YET: https://$($settings.Domain)/ has no certificate yet (see the warning above); https://$($settings.PublicIP)/ works." -ForegroundColor Yellow
        }
        Write-Host "This PC and LAN: http://localhost:3001/ and http://<WINDOWS_LAN_IP>:3001/ redirect to the HTTPS address (the router must support NAT loopback for the public IP)." -ForegroundColor Yellow
        Write-Host "Certificates renew automatically (certbot-renewer checks every 6 hours)." -ForegroundColor DarkGray
    } else {
        # V1.10.8: local uses the same stacks and gateway as prod, without HTTPS.
        Write-Host "READY (frontend HTTP): http://localhost:3001/" -ForegroundColor Green
        Write-Host "READY (frontend HTTP on LAN): http://<WINDOWS_LAN_IP>:3001/ (requires LAN firewall access)" -ForegroundColor Yellow
    }
    Write-Host "Malware Detection: sidebar > Malware Detection (API on http://localhost:8085)" -ForegroundColor Green
    Write-Host "Application build ID: $buildId" -ForegroundColor DarkGray
}
catch {
    $exitCode = 1
    Write-Host ''
    Write-Host "STARTUP ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if ("$($_.Exception.Message)" -match 'malware') {
        Write-Host 'Malware Detection diagnosis (containers, API log, gateway route): .\scripts\check-malware-stack.ps1' -ForegroundColor Yellow
    }
    if ($isPublic -and $publicGatewayStarted) {
        Write-Host 'HTTPS gateway and certificate renewal remain running; product startup did not fully complete.' -ForegroundColor Yellow
    }
    if ($isPublic) {
        Write-Host 'If the error above is HTTPS-related, check router TCP 80/443 forwarding, Windows Firewall, DNS, and ISP inbound TCP 80.' -ForegroundColor Yellow
        Write-Host 'If the error names Redis/Postgres/MinIO or another container, use that container error/log instead; HTTPS may already be healthy.' -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}

exit $exitCode
