# V1.31.8: keep this computer's Polaron client files equal to the server's.
#
# The Polaron helper on an examiner computer (port 9876), the phone collection scripts and the Python they
# run are files in a Polaron folder on that computer. Before, they changed only when someone copied a
# release there by hand, so a computer could keep an old helper long after the server was updated.
#
# -Setup     Run once by Polaron-Setup-This-Computer.cmd (downloaded from the Mobile acquisition page).
#            Finds the Polaron folder on this computer (or creates %LOCALAPPDATA%\Polaron\client), downloads
#            every client file that differs from the server, starts the helper on the new version, registers
#            the logon / 5-minute watchdog, and offers the Client Evidence Agent if it is not installed.
# -Watchdog  Run by ensure-host-drive-helper.ps1 (the watchdog), at most every 10 minutes. Downloads changed
#            files and applies them, but never while a phone collection is running.
#
# Every file is checked against the SHA-256 the server lists before it replaces anything; replaced files are
# kept in <folder>\.polaron-update\backup-<time>. Only paths under scripts\ and backend\app\ are written.
# Exit codes: 0 up to date, 10 files updated, 11 postponed (a collection is running), 2 server or setup
# problem, 3 a download did not match the server list (nothing was changed).
param(
    [string]$Server = "",
    [string]$Service = "",
    [string]$KitKey = "",
    [string]$Root = "",
    [switch]$Setup,
    [switch]$Watchdog,
    [int]$MinIntervalMinutes = 10,
    [string]$ConfigFile = "",
    [int]$HelperPort = 9876,
    [switch]$NoHelperRestart,
    [switch]$NoAgentInstall
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$sep = [IO.Path]::DirectorySeparatorChar
$localApp = $(if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { [IO.Path]::GetTempPath() })
$stateDir = Join-Path $localApp "Aetheris"
if (-not $ConfigFile) { $ConfigFile = Join-Path $stateDir "client-kit.json" }
$logFile = Join-Path (Split-Path -Parent $ConfigFile) "client-kit-update.log"

function Write-Log([string]$Text) {
    $line = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") + "  " + $Text
    if (-not $Watchdog) { Write-Host $Text }
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFile) | Out-Null
        if ((Test-Path -LiteralPath $logFile) -and (Get-Item -LiteralPath $logFile).Length -gt 1MB) {
            Move-Item -LiteralPath $logFile -Destination ($logFile + ".old") -Force
        }
        Add-Content -LiteralPath $logFile -Value $line -Encoding ASCII
    } catch {}
}

function Read-KitConfig {
    try {
        if (Test-Path -LiteralPath $ConfigFile) { return (Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json) }
    } catch {}
    return $null
}

function Write-KitConfig([hashtable]$Values) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ConfigFile) | Out-Null
    $current = @{}
    $old = Read-KitConfig
    if ($old) { foreach ($p in $old.PSObject.Properties) { $current[$p.Name] = $p.Value } }
    foreach ($k in $Values.Keys) { $current[$k] = $Values[$k] }
    [IO.File]::WriteAllText($ConfigFile, ($current | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding($false)))
}

function Get-HelperJson([string]$Path, [int]$TimeoutSec = 3) {
    $h = @{}
    if ($env:HOST_DRIVE_HELPER_TOKEN) { $h["X-Host-Helper-Token"] = $env:HOST_DRIVE_HELPER_TOKEN }
    try { return Invoke-RestMethod -UseBasicParsing -Uri ("http://127.0.0.1:" + $HelperPort + $Path) -Headers $h -TimeoutSec $TimeoutSec } catch { return $null }
}

function Test-CollectionRunning {
    # A helper restart stops a running phone collection, so updates wait for it to finish.
    $jobs = Get-HelperJson "/acquisition/jobs" 5
    if ($jobs -and @($jobs.jobs | Where-Object { [string]$_.status -eq "running" }).Count -gt 0) { return $true }
    try {
        $procs = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
        $alive = @{}
        foreach ($p in $procs) { $alive[[int]$p.ProcessId] = $true }
        foreach ($p in $procs) {
            $cmd = [string]$p.CommandLine
            if ($cmd -notmatch '(?i)ios_usbmux_backup\.py|ios_usbmux_afc\.py|native_ios_job\.ps1|native_mtp_job\.ps1|host-mobile-acquire\.ps1') { continue }
            if ($cmd -match '(?i)--verify\b|recover-stalled-ios-backup') { continue }
            # A leftover whose parent is gone belongs to a collection that was already stopped.
            if ($alive.ContainsKey([int]$p.ParentProcessId)) { return $true }
        }
    } catch {}
    return $false
}

function Resolve-ClientRoot {
    if ($Root) { return [IO.Path]::GetFullPath($Root) }
    if ($Watchdog) { return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..")) }
    $candidates = New-Object System.Collections.Generic.List[string]
    $health = Get-HelperJson "/health" 3
    if ($health -and $health.root) { $candidates.Add([string]$health.root) }
    try {
        $cmd = [string](Get-ItemProperty -LiteralPath "HKCU:\Software\Classes\aetheris-hostdrive\shell\open\command" -ErrorAction Stop)."(default)"
        if ($cmd -match '(?i)-File\s+"([^"]+)\\scripts\\[^"\\]+\.ps1"') { $candidates.Add($Matches[1]) }
    } catch {}
    try {
        $task = Get-ScheduledTask -TaskName "AetherisHostDriveHelper" -ErrorAction Stop
        foreach ($a in @($task.Actions)) {
            if ([string]$a.Arguments -match '(?i)"([^"]+)\\scripts\\ensure-host-drive-helper\.ps1"') { $candidates.Add($Matches[1]) }
        }
    } catch {}
    $cfg = Read-KitConfig
    if ($cfg -and $cfg.root) { $candidates.Add([string]$cfg.root) }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath (Join-Path $c "scripts"))) { return [IO.Path]::GetFullPath($c) }
    }
    return (Join-Path (Join-Path $localApp "Polaron") "client")
}

function Get-KitHeaders {
    $h = @{ "X-Polaron-Kit-Key" = $KitKey }
    if ($Service) { $h["X-Aetheris-Service"] = $Service }
    return $h
}

function Get-LocalPath([string]$ClientRoot, [string]$KitPath) {
    return (Join-Path $ClientRoot ($KitPath -replace '/', [string]$sep))
}

function Get-Sha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() } catch { return "" }
}

function Update-ClientFiles {
    param([string]$ClientRoot, $Manifest, [switch]$Apply)
    $changed = New-Object System.Collections.Generic.List[object]
    foreach ($f in @($Manifest.files)) {
        $kitPath = [string]$f.path
        # Only the product's own client folders; never anything else in the Polaron folder (.env, evidence, tools).
        if ($kitPath -notmatch '^(scripts|backend/app)/[A-Za-z0-9_.\-/]+$' -or $kitPath -match '\.\.') { continue }
        if ((Get-Sha256 (Get-LocalPath $ClientRoot $kitPath)) -ne ([string]$f.sha256).ToLowerInvariant()) { $changed.Add($f) }
    }
    if (-not $Apply -or $changed.Count -eq 0) { return $changed }
    $work = Join-Path $ClientRoot ".polaron-update"
    $staging = Join-Path $work "staging"
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    foreach ($f in $changed) {
        $dest = Get-LocalPath $staging ([string]$f.path)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        $uri = $Server + "/api/client-kit/file?path=" + [uri]::EscapeDataString([string]$f.path)
        Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers (Get-KitHeaders) -OutFile $dest -TimeoutSec 120
        if ((Get-Sha256 $dest) -ne ([string]$f.sha256).ToLowerInvariant()) {
            Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
            throw ("verify_failed:" + [string]$f.path)
        }
    }
    $backup = Join-Path $work ("backup-" + (Get-Date).ToString("yyyyMMdd-HHmmss"))
    foreach ($f in $changed) {
        $target = Get-LocalPath $ClientRoot ([string]$f.path)
        if (Test-Path -LiteralPath $target) {
            $keep = Get-LocalPath $backup ([string]$f.path)
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keep) | Out-Null
            Copy-Item -LiteralPath $target -Destination $keep -Force
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath (Get-LocalPath $staging ([string]$f.path)) -Destination $target -Force
    }
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    try {
        $old = @(Get-ChildItem -LiteralPath $work -Directory -Filter "backup-*" | Sort-Object Name -Descending | Select-Object -Skip 3)
        foreach ($d in $old) { Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    } catch {}
    return $changed
}

# --- settings -------------------------------------------------------------------------------------------
$cfg = Read-KitConfig
if ($Watchdog) {
    if (-not $cfg -or -not $cfg.server -or -not $cfg.kit_key) { exit 0 }   # this computer was not set up for updates
    if (-not $Server) { $Server = [string]$cfg.server }
    if (-not $KitKey) { $KitKey = [string]$cfg.kit_key }
    if (-not $Service) { $Service = [string]$cfg.service }
    $last = [DateTime]::MinValue
    if ($cfg.checked_utc) { try { $last = [DateTime]::Parse([string]$cfg.checked_utc).ToUniversalTime() } catch {} }
    if (((Get-Date).ToUniversalTime() - $last).TotalMinutes -lt $MinIntervalMinutes) { exit 0 }
} elseif ($cfg) {
    if (-not $Server) { $Server = [string]$cfg.server }
    if (-not $KitKey) { $KitKey = [string]$cfg.kit_key }
    if (-not $Service) { $Service = [string]$cfg.service }
}
$Server = ([string]$Server).Trim().TrimEnd('/')
if ($Server -notmatch '^https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?$' -or -not $KitKey) {
    Write-Log "Polaron server address or kit key missing. Download Polaron-Setup-This-Computer.cmd from the Mobile acquisition page and run it."
    exit 2
}
$clientRoot = Resolve-ClientRoot
if ($Setup) {
    Write-Log ("Polaron folder on this computer: " + $clientRoot)
}

# --- compare with the server ------------------------------------------------------------------------------
try {
    $manifest = Invoke-RestMethod -UseBasicParsing -Uri ($Server + "/api/client-kit/manifest") -Headers (Get-KitHeaders) -TimeoutSec $(if ($Watchdog) { 20 } else { 60 })
} catch {
    Write-Log ("Could not reach the Polaron server " + $Server + ": " + $_.Exception.Message)
    exit 2
}
$pending = @(Update-ClientFiles -ClientRoot $clientRoot -Manifest $manifest)
$now = (Get-Date).ToUniversalTime().ToString("o")
$configValues = @{ server = $Server; service = $Service; kit_key = $KitKey; root = $clientRoot; checked_utc = $now; server_helper_version = [string]$manifest.helper_version }

$updated = 0
if ($pending.Count -gt 0) {
    if (Test-CollectionRunning) {
        Write-Log ("" + $pending.Count + " Polaron file(s) differ from the server, but a phone collection is running on this computer. The update waits until it has finished.")
        if ($Setup) { Write-Host "Let the collection finish (or stop it), then run this file again." -ForegroundColor Yellow }
        else { Write-KitConfig $configValues }
        exit 11
    }
    Write-Log ("Updating " + $pending.Count + " Polaron file(s) from " + $Server + " (helper " + [string]$manifest.helper_version + ")...")
    try {
        $done = @(Update-ClientFiles -ClientRoot $clientRoot -Manifest $manifest -Apply)
        $updated = $done.Count
    } catch {
        $msg = [string]$_.Exception.Message
        if ($msg -like "verify_failed:*") {
            Write-Log ("A downloaded file did not match the server list (" + $msg.Substring(14) + "); nothing was changed.")
            exit 3
        }
        Write-Log ("The update could not be downloaded: " + $msg + ". Nothing was changed.")
        exit 2
    }
    $configValues["updated_utc"] = $now
    $configValues["kit_version"] = [string]$manifest.kit_version
    Write-Log ("Updated " + $updated + " file(s). Kit " + [string]$manifest.kit_version + ".")
} else {
    $configValues["kit_version"] = [string]$manifest.kit_version
    if ($Setup) { Write-Log "The Polaron files on this computer already match the server." }
}
Write-KitConfig $configValues

if ($Watchdog) { if ($updated -gt 0) { exit 10 } else { exit 0 } }

# --- setup: helper on the new version, watchdog, Client Evidence Agent ---------------------------------------
if (-not $NoHelperRestart) {
    $taskScript = Join-Path $clientRoot "scripts\install-host-drive-helper-task.ps1"
    $ensure = Join-Path $clientRoot "scripts\ensure-host-drive-helper.ps1"
    $psExe = "powershell.exe"
    if ($env:SystemRoot) {
        $winPs = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        if (Test-Path -LiteralPath $winPs) { $psExe = $winPs }
    }
    Write-Host "Starting the Polaron helper on the new version (this can take a few minutes the first time)..."
    try {
        # Registers the logon / 5-minute watchdog, which also checks the server for updates from now on.
        if (Test-Path -LiteralPath $taskScript) { & $psExe -NoProfile -ExecutionPolicy Bypass -File $taskScript }
        & $psExe -NoProfile -ExecutionPolicy Bypass -File $ensure -SkipKitUpdate
    } catch {
        Write-Log ("Starting the helper reported: " + $_.Exception.Message)
    }
    $health = $null
    for ($i = 0; $i -lt 40; $i++) {
        $health = Get-HelperJson "/health" 3
        if ($health -and [string]$health.helper_version -eq [string]$manifest.helper_version) { break }
        Start-Sleep -Seconds 3
    }
    if ($health -and [string]$health.helper_version -eq [string]$manifest.helper_version) {
        Write-Host ("Polaron helper " + [string]$health.helper_version + " is running on this computer.") -ForegroundColor Green
    } else {
        $seen = $(if ($health) { [string]$health.helper_version } else { "not answering" })
        Write-Host ("The Polaron helper is " + $seen + "; expected " + [string]$manifest.helper_version + ". Run scripts\ensure-host-drive-helper.ps1 -ForceRestart in " + $clientRoot + " when no collection is running.") -ForegroundColor Yellow
    }

    if (-not $NoAgentInstall) {
        $agentUp = $false
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $ar = $tcp.BeginConnect("127.0.0.1", 9877, $null, $null)
            $agentUp = $ar.AsyncWaitHandle.WaitOne(500) -and $tcp.Connected
            $tcp.Close()
        } catch {}
        $agentScript = Join-Path $clientRoot "scripts\client-evidence-agent\polaron-client-evidence-agent.ps1"
        if (-not $agentUp -and (Test-Path -LiteralPath $agentScript)) {
            Write-Host ""
            Write-Host "The Polaron Client Evidence Agent is not running on this computer. It lets the forensic server copy"
            Write-Host "phone images made here and read evidence folders you choose (read-only). Windows asks for administrator"
            Write-Host "approval once."
            $answer = Read-Host "Install it now? [Y/n]"
            if ($answer -notmatch '^(n|no)$') {
                try { & $psExe -NoProfile -ExecutionPolicy Bypass -File $agentScript -Install -ConsoleOrigin $Server } catch { Write-Log ("Agent install: " + $_.Exception.Message) }
            }
        }
    }
}
Write-Host ""
Write-Host "Done. Return to Polaron and click Rescan on the acquisition page." -ForegroundColor Green
exit 0
