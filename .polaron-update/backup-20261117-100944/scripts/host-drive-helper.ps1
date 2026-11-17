# Localhost helper for the Forensic UI - regenerates drive bind mounts and recreates api/worker.
# Run on the Windows examiner workstation (not inside Docker):
#   powershell -ExecutionPolicy Bypass -File scripts/host-drive-helper.ps1
#
# Optional env:
#   HOST_DRIVE_HELPER_PORT=9876
#   HOST_DRIVE_HELPER_BIND_HOST=+    (+ lets Docker Desktop reach the Windows host helper)
#   HOST_DRIVE_HELPER_TOKEN=secret   (require X-Host-Helper-Token header when set)

param(
    [int]$Port = $(if ($env:HOST_DRIVE_HELPER_PORT) { [int]$env:HOST_DRIVE_HELPER_PORT } else { 9876 }),
    [string]$BindHost = $(if ($env:HOST_DRIVE_HELPER_BIND_HOST) { [string]$env:HOST_DRIVE_HELPER_BIND_HOST } else { "+" })
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$generateScript = Join-Path $root "scripts\generate-drive-mounts.ps1"
$composeBase = Join-Path $root "docker-compose.yml"
$composeDrives = Join-Path $root "docker-compose.drives.generated.yml"
$expectedToken = $env:HOST_DRIVE_HELPER_TOKEN
$acquireScript = Join-Path $root "scripts\host-mobile-acquire.ps1"
if (Test-Path -LiteralPath $acquireScript) {
    . $acquireScript
}

function Test-PythonExe {
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        $tag = [guid]::NewGuid().ToString("N")
        $out = Join-Path $env:TEMP ("aetheris_pyok_" + $tag + ".out")
        $err = Join-Path $env:TEMP ("aetheris_pyok_" + $tag + ".err")
        $proc = Start-Process -FilePath $Exe -ArgumentList @("-c", "print(1)") -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $null = $proc.Handle  # Windows PowerShell 5.1: without a cached handle ExitCode reads as empty after exit
        if (-not $proc.WaitForExit(6000)) {  # a cold venv start with antivirus scanning often exceeds 2 s
            try { $proc.Kill() } catch {}
            Remove-Item -LiteralPath $out, $err -Force -ErrorAction SilentlyContinue
            return $false
        }
        $ok = ($proc.ExitCode -eq 0)
        Remove-Item -LiteralPath $out, $err -Force -ErrorAction SilentlyContinue
        return $ok
    } catch { return $false }
}

# Malware Detection live triage (read-only collection from a connected phone)
. (Join-Path $PSScriptRoot 'host-malware-triage.ps1')

function Get-HostPython {
    foreach ($c in @(
        (Join-Path $root "backend\.venv\Scripts\python.exe"),
        (Join-Path $root ".venv\Scripts\python.exe"),
        (Join-Path $root "tools\host-python\Scripts\python.exe")
    )) {
        if ($c -match '(?i)WindowsApps') { continue }
        if (Test-PythonExe $c) { return $c }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and $cmd.Source -notmatch '(?i)WindowsApps' -and (Test-PythonExe $cmd.Source)) {
        return $cmd.Source
    }
    return $null
}

function New-HostDevicePreview {
    param([string]$Adapter, [string]$DeviceId, [string]$NameHint = "")
    $os = "android"
    if ($Adapter -match 'ios') { $os = "ios" }
    $name = $(if ($NameHint) { $NameHint } else { $DeviceId })
    if ($name -match '(?i)iPhone|iPad|iPod|Apple') { $os = "ios" }
    elseif ($name -match '(?i)Android|Samsung|Galaxy|Pixel|Xiaomi|Huawei|Honor|OnePlus|Oppo|Vivo') { $os = "android" }
    $logicalOnly = ($Adapter -match 'mtp' -or $Adapter -eq "android_mtp")
    $methods = if ($os -eq "ios") { @("backup", "advanced_logical", "logical") } else { @("logical", "advanced_logical", "backup", "file_system", "full_file_system") }
    $manufacturer = if ($os -eq "ios") { "Apple" } elseif ($name -match '(?i)Samsung|Galaxy') { "Samsung" } elseif ($name -match '(?i)Oppo') { "OPPO" } else { "" }
    $lockState = if ($logicalOnly) { "unlocked_for_mtp" } else { "unknown" }
    $label = "SUPPORTED_DIRECT_LOGICAL"
    return @{
        ok = $true
        device_profile = @{
            manufacturer = $manufacturer
            model = $name
            device_label = $name
            chipset = ""
            serial = $DeviceId
            imei = ""
            udid = $(if ($os -eq "ios") { $DeviceId } else { "" })
            os_family = $os
            os_version = ""
            security_patch_level = ""
            build_id = ""
            lock_state = $lockState
            encryption_state = "unknown"
            connection_mode = $(if ($logicalOnly) { "mtp" } else { "usb" })
            usb_debugging_authorized = [bool]($Adapter -eq "android_adb")
            pairing_trusted = [bool]($os -eq "ios")
            developer_mode = $false
            rooted_or_jailbroken = $null
            battery_percent = $null
            network_isolated = $null
            sim_present = $null
            iccid = ""
            imsi = ""
            sd_card_present = $null
            sd_card_identifier = ""
            tool_version = "aetheris-acquire/host-helper"
            license_entitlements = @()
            required_cable = "USB data cable"
            unknown_fields = @()
            observations = @(
                "Detected on the examiner Windows host over USB (no extra adapter).",
                $(if ($logicalOnly) { "MTP / file-transfer collection of shared storage, recycle-bin folders, and chat media. Enable USB debugging for ADB backup of app databases." } else { "Host USB collection via the HostDrive helper." })
            )
        }
        capability = @{
            capability_label = $label
            supported_methods = $methods
            blocked_methods = @{}
            warnings = @()
            preparation_steps = @(
                "Keep the phone unlocked and the data cable seated.",
                $(if ($os -eq "ios") { "Tap Trust This Computer if prompted." } else { "Use File transfer / MTP if the phone asks." })
            )
        }
        preparation_steps = @(
            "Keep the phone unlocked and the data cable seated.",
            $(if ($os -eq "ios") { "Tap Trust This Computer if prompted." } else { "Use File transfer / MTP if the phone asks." })
        )
        method_profiles = @{}
    }
}

function Write-AcquireJobSidecar {
    param(
        [string]$JobId,
        [int]$ProcessId,
        [string]$ResultFile,
        [string]$ProgressFile,
        [string]$CaseId = ""
    )
    $dir = Join-Path $env:TEMP "aetheris_acq_jobs"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $meta = @{
        pid           = $ProcessId
        result_file   = $ResultFile
        progress_file = $ProgressFile
        status        = "running"
        started_utc   = (Get-Date).ToUniversalTime().ToString("o")
        case_id       = $CaseId
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $dir ($JobId + ".json")),
        ($meta | ConvertTo-Json -Compress),
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Start-NativeMtpAcquireJob {
    param(
        [hashtable]$RunPayload,
        [string]$Stamp,
        [string]$TmpResult
    )
    $jobPs1 = Join-Path $root "scripts\native_mtp_job.ps1"
    $payloadFile = Join-Path $env:TEMP ("acq_mtp_" + $Stamp + ".json")
    [System.IO.File]::WriteAllText($payloadFile, ($RunPayload | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
    $script:Busy = $true
    $script:AcquireBusy = $true
    $argList = @(
        "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $jobPs1,
        "-PayloadFile", $payloadFile,
        "-ResultFile", $TmpResult
    )
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WorkingDirectory $root `
        -WindowStyle Minimized -PassThru
    Write-AcquireJobSidecar -JobId $Stamp -ProcessId $proc.Id -ResultFile $TmpResult -ProgressFile ([string]$RunPayload.progress_file) -CaseId ([string]$RunPayload.case_id)
    $script:AcquireJobs[$Stamp] = @{
        Pid          = $proc.Id
        ResultFile   = $TmpResult
        ProgressFile = [string]$RunPayload.progress_file
        ErrFile      = ""
        OutFile      = ""
        Status       = "running"
        Error        = ""
        StartedUtc   = (Get-Date).ToUniversalTime().ToString("o")
    }
}

function Find-AdbExe {
    $wingetRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $candidates = @(
        (Join-Path $root "tools\platform-tools\adb.exe"),
        (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\adb.exe"),
        "C:\Android\platform-tools\adb.exe",
        "C:\platform-tools\adb.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    if (Test-Path -LiteralPath $wingetRoot) {
        $hit = Get-ChildItem -LiteralPath $wingetRoot -Filter "adb.exe" -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    $cmd = Get-Command "adb.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-IosUsbmuxDevices {
    $script:LastUsbmuxError = $null
    $listScript = Join-Path $root "scripts\ios_usbmux_list.py"
    $py = Get-HostPython
    if (-not $py -or -not (Test-Path -LiteralPath $listScript)) {
        $script:LastUsbmuxError = "examiner kit python or ios_usbmux_list.py missing"
        return @()
    }
    try {
        $prev = $env:PYTHONPATH
        $env:PYTHONPATH = (Join-Path $root "backend")
        $raw = & $py $listScript 2>$null | Out-String
        $env:PYTHONPATH = $prev
        if (-not $raw) { return @() }
        $jsonLine = ($raw -split "`r?`n" | Where-Object { $_.Trim().StartsWith("{") } | Select-Object -Last 1)
        if (-not $jsonLine) { return @() }
        $obj = $jsonLine | ConvertFrom-Json
        if ($obj.error) { $script:LastUsbmuxError = [string]$obj.error }
        if (-not $obj.ok) { return @() }
        return @($obj.devices)
    }
    catch {
        $script:LastUsbmuxError = $_.Exception.Message
        return @()
    }
}

function Find-IdeviceIdExe {
    $candidates = @(
        (Join-Path $root "tools\libimobiledevice\idevice_id.exe"),
        "C:\Program Files\libimobiledevice\idevice_id.exe",
        "C:\tools\libimobiledevice\idevice_id.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    if (Get-Command Find-HostTool -ErrorAction SilentlyContinue) {
        $found = Find-HostTool -Names @("idevice_id.exe", "idevice_id")
        if ($found) { return $found }
    }
    $cmd = Get-Command "idevice_id.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$script:LastUsbmuxError = $null
$script:Busy = $false
$script:AcquireBusy = $false
$script:PendingAcquire = $null
# Async acquire jobs: job_id -> @{ Pid; ResultFile; ProgressFile; Status; Error; StartedUtc }
$script:AcquireJobs = @{}
# Async drive-mount refresh (keeps /health responsive while docker compose runs)
$script:RefreshStatusFile = Join-Path $env:TEMP ("aetheris_drive_refresh_{0}.json" -f $PID)
$script:RefreshProcess = $null
$refreshJobScript = Join-Path $root "scripts\refresh-drive-mounts-job.ps1"
$script:HelperComputerName = [string]$env:COMPUTERNAME
# Where a client-created mobile image is copied on this server (V1.27.0). Default:
# the same case root server-side collections use. HOST_IMPORT_CASE_ROOT overrides it.
$script:ImportCasesRootOverride = [string]$env:HOST_IMPORT_CASE_ROOT
$script:ImportCasesRoot = $(if ($script:ImportCasesRootOverride) { $script:ImportCasesRootOverride } else { Join-Path $root "evidence\cases" })
$script:HelperVersion = "5.64"  # 5.64 phone lists read live from Windows on each request (no frozen list); 5.63 /health says whether the Client Evidence Agent runs; kept up to date from the server (polaron-client-update.ps1); 5.62 Android phones found by their File transfer interface; charging-only / no-driver phones listed with what to do; 5.61 phone list follows the cables during a copy (registry read) and ignores leftover copy processes; 5.60 do not kill pnputil or re-enumerate USB during a phone copy; 5.59 C# taskkill fallbacks keep `$_ (stop USB collection works)
$script:DeviceCacheAt = [datetime]::MinValue
$script:DeviceCache = @()
$script:DeviceScanBusy = $false
$script:DeviceCacheFile = Join-Path $env:TEMP "aetheris_host_mobile_devices.json"
$script:AdaptersCacheFile = Join-Path $env:TEMP "aetheris_host_acquisition_adapters.json"
$script:AcqDevicesCacheFile = Join-Path $env:TEMP "aetheris_host_acquisition_devices.json"
$script:DeviceRefreshJob = $null
$script:DriveCacheAt = [datetime]::MinValue
$script:DriveCache = @()
$script:HelperStartedUtc = (Get-Date).ToUniversalTime().ToString("o")

function Get-JobScriptVersion {
    # Read the version marker from the job script currently on disk so /health
    # reflects what a refresh would actually run.
    try {
        if (-not (Test-Path -LiteralPath $refreshJobScript)) { return $null }
        $head = Get-Content -LiteralPath $refreshJobScript -TotalCount 40 -ErrorAction Stop
        foreach ($line in @($head)) {
            if (("$line") -match 'AETHERIS_JOB_VERSION:\s*([0-9][0-9A-Za-z.\-]*)') { return $Matches[1] }
        }
        return "legacy"
    }
    catch { return $null }
}

function Write-RawJsonResponse {
    param(
        [System.Net.HttpListenerResponse]$Response,
        [int]$StatusCode,
        [string]$Json
    )

    if ($null -eq $Json) { $Json = "{}" }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Json)
    $Response.StatusCode = $StatusCode
    $Response.ContentType = "application/json; charset=utf-8"
    $Response.Headers["Access-Control-Allow-Origin"] = "*"
    $Response.Headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    $Response.Headers["Access-Control-Allow-Headers"] = "Content-Type, X-Host-Helper-Token"
    $Response.ContentLength64 = $bytes.Length
    $Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $Response.OutputStream.Close()
}

function Write-JsonResponse {
    param(
        [System.Net.HttpListenerResponse]$Response,
        [int]$StatusCode,
        [object]$Body
    )

    # Depth 20 for nested cloud_mailboxes / evidence packages; still avoid for huge acquire results.
    $json = $Body | ConvertTo-Json -Compress -Depth 20
    Write-RawJsonResponse -Response $Response -StatusCode $StatusCode -Json $json
}

function Stop-AcquireJob {
    param(
        [string]$JobId = "",
        [string]$ProgressFile = ""
    )

    $stopped = New-Object System.Collections.Generic.List[string]
    $ids = @()
    if ($JobId -and $script:AcquireJobs.ContainsKey($JobId)) {
        $ids = @($JobId)
    }
    else {
        $ids = @($script:AcquireJobs.Keys | Where-Object {
            $script:AcquireJobs[$_].Status -eq "running"
        })
    }

    foreach ($id in $ids) {
        $job = $script:AcquireJobs[$id]
        $prog = [string]$job.ProgressFile
        if ($ProgressFile) { $prog = $ProgressFile }
        if ($prog) {
            try {
                Set-Content -LiteralPath ($prog + ".cancel") -Value "cancel" -Encoding UTF8
            } catch {}
            try {
                @{
                    stage      = "cancelled"
                    item       = "cancelled"
                    detail     = "Stopped by examiner"
                    category   = "Cancelled"
                    bytes_done = 0
                } | ConvertTo-Json -Compress | Set-Content -LiteralPath $prog -Encoding UTF8
            } catch {}
        }
        if ($job.Pid) {
            & taskkill.exe /PID ([int]$job.Pid) /T /F 2>$null | Out-Null
        }
        $job.Status = "cancelled"
        $script:AcquireJobs[$id] = $job
        $stopped.Add($id) | Out-Null
    }

    # Orphans from a previous helper still writing an iPhone backup.
    try {
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'acq_run_|ios_usbmux_backup|native_ios_job|native_mtp_job|host-mobile-acquire' } |
            ForEach-Object {
                & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null
            }
    } catch {}
    try {
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine -notmatch 'host-drive-helper' -and
                $_.CommandLine -match 'native_ios_job|native_mtp_job|host-mobile-acquire'
            } |
            ForEach-Object {
                & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null
            }
    } catch {}

    # Native C# sidecar jobs (MTP / iOS) are not in $script:AcquireJobs.
    $jobsDir = Join-Path $env:TEMP "aetheris_acq_jobs"
    if (Test-Path -LiteralPath $jobsDir) {
        $sidecarFiles = @()
        if ($JobId) {
            $one = Join-Path $jobsDir ($JobId + ".json")
            if (Test-Path -LiteralPath $one) { $sidecarFiles = @($one) }
        } else {
            $sidecarFiles = @(Get-ChildItem -LiteralPath $jobsDir -Filter "*.json" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        }
        foreach ($metaPath in $sidecarFiles) {
            try {
                $meta = Get-Content -LiteralPath $metaPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $sid = [int]($(if ($meta.pid) { $meta.pid } else { 0 }))
                if ($sid -gt 0) {
                    $alive = Get-Process -Id $sid -ErrorAction SilentlyContinue
                    if ($alive) {
                        & taskkill.exe /PID $sid /T /F 2>$null | Out-Null
                    }
                    $stopped.Add(([IO.Path]::GetFileNameWithoutExtension($metaPath))) | Out-Null
                }
                $prog = [string]$meta.progress_file
                if ($prog) {
                    try {
                        Set-Content -LiteralPath ($prog + ".cancel") -Value "cancel" -Encoding UTF8
                    } catch {}
                }
            } catch {}
        }
    }

    $script:Busy = $false
    $script:AcquireBusy = $false
    return @{
        ok        = $true
        cancelled = @($stopped)
        message   = "Host collection stopped"
    }
}

function _Add-SafeAcquirePath {
    param($Bag, [string]$Path)
    if (-not $Path) { return }
    $p = [string]$Path
    if ($p.Length -lt 8) { return }
    if ($p -match '^[\\/]evidence') { return }
    if (-not (Test-Path -LiteralPath $p)) { return }
    [void]$Bag.Add($p)
}

function Remove-AcquireRunData {
    param(
        [string]$JobId = "",
        [string]$RunName = "",
        [string]$CaseId = "",
        [string]$ProgressFile = "",
        [hashtable]$ExtraPaths = @{}
    )
    $stop = Stop-AcquireJob -JobId $JobId -ProgressFile $ProgressFile
    $removed = New-Object System.Collections.Generic.List[string]
    $candidates = New-Object System.Collections.Generic.List[string]

    $job = $null
    if ($JobId -and $script:AcquireJobs.ContainsKey($JobId)) {
        $job = $script:AcquireJobs[$JobId]
    }
    $progFile = $ProgressFile
    $resultFile = ""
    if ($job) {
        if (-not $progFile) { $progFile = [string]$job.ProgressFile }
        $resultFile = [string]$job.ResultFile
    }
    $sidecar = Join-Path (Join-Path $env:TEMP "aetheris_acq_jobs") ($JobId + ".json")
    if ((-not $progFile -or -not $resultFile) -and $JobId -and (Test-Path -LiteralPath $sidecar)) {
        try {
            $meta = Get-Content -LiteralPath $sidecar -Raw -Encoding UTF8 | ConvertFrom-Json
            if (-not $progFile) { $progFile = [string]$meta.progress_file }
            if (-not $resultFile) { $resultFile = [string]$meta.result_file }
            if (-not $CaseId) { $CaseId = [string]$meta.case_id }
        } catch {}
    }

    $objs = @()
    foreach ($f in @($progFile, $resultFile)) {
        if ($f -and (Test-Path -LiteralPath $f)) {
            try { $objs += (Get-Content -LiteralPath $f -Raw -Encoding UTF8 | ConvertFrom-Json) } catch {}
        }
    }
    foreach ($o in $objs) {
        if (-not $RunName -and $o.run_name) { $RunName = [string]$o.run_name }
        $paths = $o.paths
        if ($paths) {
            foreach ($k in @("original", "working", "logs", "hashes", "exports", "case_root")) {
                _Add-SafeAcquirePath $candidates ([string]$paths.$k)
            }
        }
        foreach ($k in @("output_path", "media_path", "case_path")) {
            if ($o.$k) { _Add-SafeAcquirePath $candidates ([string]$o.$k) }
            if ($o.progress -and $o.progress.$k) { _Add-SafeAcquirePath $candidates ([string]$o.progress.$k) }
        }
        $serial = ""
        if ($o.device -and $o.device.serial) { $serial = [string]$o.device.serial }
        if (-not $serial -and $o.device_id) { $serial = [string]$o.device_id }
        $digits = ($serial -replace '[^0-9A-Fa-f]', '')
        if ($digits.Length -ge 8) {
            $tail = $digits.Substring($digits.Length - 8)
            foreach ($letter in @("E", "D", "C")) {
                _Add-SafeAcquirePath $candidates (Join-Path "${letter}:\ib" $tail)
            }
        }
    }
    foreach ($k in @("original", "working", "logs", "hashes", "exports", "case_root", "output_path")) {
        if ($ExtraPaths.$k) { _Add-SafeAcquirePath $candidates ([string]$ExtraPaths.$k) }
    }

    if ($RunName) {
        $safeName = $RunName -replace '[^\w\-.]', '_'
        foreach ($base in $candidates) {
            $parent = if (Test-Path -LiteralPath $base -PathType Container) { $base } else { Split-Path -Parent $base }
            $caseDir = $parent
            for ($i = 0; $i -lt 4; $i++) {
                if ((Split-Path -Leaf $caseDir) -match '^CASE-') { break }
                $up = Split-Path -Parent $caseDir
                if (-not $up -or $up -eq $caseDir) { break }
                $caseDir = $up
            }
            foreach ($sub in @("02_Original_Extraction", "03_Working_Copy", "05_Exports", "07_Logs", "08_Hashes")) {
                _Add-SafeAcquirePath $candidates (Join-Path (Join-Path $caseDir $sub) $safeName)
            }
        }
        if ($CaseId) {
            $caseRoot = Join-Path (Join-Path $root "evidence\cases") ($CaseId -replace '[^\w\-.]', '_')
            foreach ($sub in @("02_Original_Extraction", "03_Working_Copy", "05_Exports", "07_Logs", "08_Hashes")) {
                _Add-SafeAcquirePath $candidates (Join-Path (Join-Path $caseRoot $sub) $safeName)
            }
        }
    }

    foreach ($p in @($candidates | Select-Object -Unique)) {
        if (-not $p) { continue }
        $leaf = Split-Path -Leaf $p
        $allow = $false
        if ($RunName -and ($leaf -eq $RunName -or $p -match [regex]::Escape($RunName))) { $allow = $true }
        if ($JobId -and ($leaf -eq $JobId -or $p -match [regex]::Escape($JobId))) { $allow = $true }
        if ($p -match '[\\/]ib[\\/][0-9A-Fa-f]{6,}') { $allow = $true }
        if ($leaf -match '^(02_|03_|05_|07_|08_|CASE-)') { $allow = $false }
        if (-not $allow) { continue }
        try {
            if (Test-Path -LiteralPath $p) {
                cmd.exe /c "rmdir /s /q `"$p`"" | Out-Null
                if (Test-Path -LiteralPath $p) {
                    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
            if (-not (Test-Path -LiteralPath $p)) { $removed.Add($p) | Out-Null }
        } catch {}
    }

    if ($JobId) {
        foreach ($pat in @(
            (Join-Path $env:TEMP ("acq_ios_" + $JobId + ".*")),
            (Join-Path $env:TEMP ("acq_mtp_" + $JobId + ".*")),
            (Join-Path $env:TEMP ("acq_result_" + $JobId + ".*")),
            (Join-Path $env:TEMP ("acq_progress_" + $JobId + ".*"))
        )) {
            Get-Item -Path $pat -ErrorAction SilentlyContinue | ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
        }
        if (Test-Path -LiteralPath $sidecar) {
            Remove-Item -LiteralPath $sidecar -Force -ErrorAction SilentlyContinue
        }
    }
    if ($progFile) {
        Remove-Item -LiteralPath $progFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath ($progFile + ".cancel") -Force -ErrorAction SilentlyContinue
    }
    if ($resultFile) { Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue }
    if ($JobId -and $script:AcquireJobs.ContainsKey($JobId)) { $script:AcquireJobs.Remove($JobId) }

    return @{
        ok        = $true
        cancelled = $stop.cancelled
        removed   = @($removed)
        message   = "Collection stopped and this run's files were removed"
    }
}

function Get-SidecarRunningJobIds {
    $ids = New-Object System.Collections.Generic.List[string]
    $jobsDir = Join-Path $env:TEMP "aetheris_acq_jobs"
    if (-not (Test-Path -LiteralPath $jobsDir)) { return @() }
    foreach ($f in @(Get-ChildItem -LiteralPath $jobsDir -Filter "*.json" -ErrorAction SilentlyContinue)) {
        try {
            $meta = Get-Content -LiteralPath $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            $sid = [int]($(if ($meta.pid) { $meta.pid } else { 0 }))
            if ($sid -le 0) { continue }
            $rf = [string]$meta.result_file
            if ($rf -and (Test-Path -LiteralPath $rf)) {
                $head = ""
                try { $head = Get-Content -LiteralPath $rf -TotalCount 8 -Raw -ErrorAction SilentlyContinue } catch {}
                if ($head -and $head -match '"ok"' -and $head -notmatch '"stage_reached"\s*:\s*"acquire"') { continue }
            }
            $prog = [string]$meta.progress_file
            if ($prog -and ((Test-Path -LiteralPath ($prog + ".cancel")) -or (Test-Path -LiteralPath $prog))) {
                if (Test-Path -LiteralPath ($prog + ".cancel")) { continue }
                try {
                    $pj = Get-Content -LiteralPath $prog -Raw -ErrorAction SilentlyContinue
                    if ($pj -and $pj -match '"stage"\s*:\s*"cancelled"') { continue }
                } catch {}
            }
            if (Get-Process -Id $sid -ErrorAction SilentlyContinue) {
                $ids.Add($f.BaseName) | Out-Null
            }
        } catch {}
    }
    return @($ids)
}

function Get-RunningUsbJobIds {
    $ids = New-Object System.Collections.Generic.List[string]
    foreach ($id in @($script:AcquireJobs.Keys)) {
        $job = $script:AcquireJobs[$id]
        if ($job.Status -ne "running") { continue }
        if ($job.Pid) {
            $proc = Get-Process -Id ([int]$job.Pid) -ErrorAction SilentlyContinue
            if ($proc) { $ids.Add($id) | Out-Null }
        }
    }
    foreach ($sid in @(Get-SidecarRunningJobIds)) {
        if (-not $ids.Contains($sid)) { $ids.Add($sid) | Out-Null }
    }
    return @($ids)
}

function Write-UsbBusyResponse {
    param($Response)
    Write-JsonResponse -Response $Response -StatusCode 409 -Body @{
        ok              = $false
        error           = "Another USB collection is already running. Stop it, wait for it to finish, or register an existing backup folder for forensic analysis."
        busy            = $true
        running_job_ids = @(Get-RunningUsbJobIds)
    }
}

function Test-UsbAcquireInProgress {
    # True only when an async USB acquire process is still alive.
    # The HTTP listener is single-threaded, so a sync /acquire cannot overlap
    # another request. Drive-mount refresh must not block forensic browse,
    # folder register, or a new job on the same phone after a failed acquire.
    $alive = $false
    foreach ($id in @($script:AcquireJobs.Keys)) {
        $job = $script:AcquireJobs[$id]
        if ($job.Status -ne "running") { continue }
        if ($job.Pid) {
            $proc = Get-Process -Id ([int]$job.Pid) -ErrorAction SilentlyContinue
            if ($proc) { $alive = $true; continue }
        }
        Get-AcquireJobStatus -JobId $id | Out-Null
    }
    if (-not $alive) {
        $sidecar = @(Get-SidecarRunningJobIds)
        if ($sidecar.Count -gt 0) { $alive = $true }
    }
    if (-not $alive) {
        $script:AcquireBusy = $false
        $refreshAlive = $false
        if ($script:RefreshProcess) {
            try { $refreshAlive = -not $script:RefreshProcess.HasExited } catch { $refreshAlive = $false }
        }
        if (-not $refreshAlive) { $script:Busy = $false }
    }
    return $alive
}

function Get-AcquireJobStatus {
    param([string]$JobId)

    if (-not $JobId -or -not $script:AcquireJobs.ContainsKey($JobId)) {
        return @{ ok = $false; status = "not_found"; job_id = $JobId; error = "Unknown job id" }
    }
    $job = $script:AcquireJobs[$JobId]
    $alive = $false
    if ($job.Pid) {
        $proc = Get-Process -Id ([int]$job.Pid) -ErrorAction SilentlyContinue
        if ($proc) { $alive = $true }
    }
    if (-not $alive -and $job.Status -in @("cancelled", "completed", "failed")) {
        return @{
            ok      = ($job.Status -ne "failed")
            status  = $job.Status
            job_id  = $JobId
            result  = @{
                ok            = ($job.Status -eq "completed")
                error         = $(if ($job.Status -eq "cancelled") { "Cancelled by examiner" } else { $job.Error })
                errors        = @($(if ($job.Status -eq "cancelled") { "Cancelled by examiner" } else { $job.Error }))
                stage_reached = $job.Status
                paths         = @{}
            }
        }
    }
    if ((Test-Path -LiteralPath $job.ResultFile) -and (-not $alive)) {
        $job.Status = "completed"
        $peekOk = $true
        $runName = ""
        try {
            $fs = [System.IO.File]::OpenRead($job.ResultFile)
            try {
                $buf = New-Object byte[] 2048
                $n = $fs.Read($buf, 0, $buf.Length)
                $head = [System.Text.Encoding]::UTF8.GetString($buf, 0, $n)
                if ($head -match '"ok"\s*:\s*false') { $peekOk = $false; $job.Status = "failed" }
                if ($head -match '"run_name"\s*:\s*"([^"]+)"') { $runName = $Matches[1] }
            } finally { $fs.Close() }
        } catch {}
        $script:AcquireJobs[$JobId] = $job
        $script:Busy = $false
        $script:AcquireBusy = $false
        return @{
            ok          = $true
            status      = $job.Status
            job_id      = $JobId
            result_file = $job.ResultFile
            result      = @{
                ok            = $peekOk
                run_name      = $runName
                stage_reached = $job.Status
            }
        }
    }
    if (-not $alive -and $job.Status -eq "running") {
        $errText = ""
        if ($job.ErrFile -and (Test-Path -LiteralPath $job.ErrFile)) {
            $errText = Get-Content -LiteralPath $job.ErrFile -Raw -EA SilentlyContinue
        }
        $job.Status = "failed"
        $job.Error = if ($errText) { $errText.Trim().Substring(0, [Math]::Min(500, $errText.Trim().Length)) } else { "Acquire process exited without result" }
        $script:AcquireJobs[$JobId] = $job
        $script:Busy = $false
        $script:AcquireBusy = $false
        return @{
            ok     = $false
            status = "failed"
            job_id = $JobId
            error  = $job.Error
            result = @{
                ok            = $false
                error         = $job.Error
                errors        = @($job.Error)
                stage_reached = "failed"
                paths         = @{}
            }
        }
    }
    $detail = ""
    $runName = ""
    $files = 0
    $bytes = [int64]0
    $pct = $null
    if ($job.ProgressFile -and (Test-Path -LiteralPath $job.ProgressFile)) {
        try {
            $p = Get-Content -LiteralPath $job.ProgressFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $detail = [string]$p.detail
            $runName = [string]$p.run_name
            $files = [int]$p.files_seen
            $bytes = [int64]$p.bytes_done
            if ($p.progress_pct -ne $null) { $pct = [double]$p.progress_pct }
        } catch {}
    }
    return @{
        ok           = $true
        status       = "running"
        job_id       = $JobId
        busy         = $true
        progress     = $job.ProgressFile
        started      = $job.StartedUtc
        detail       = $detail
        run_name     = $runName
        files_seen   = $files
        bytes_done   = $bytes
        progress_pct = $pct
        result       = @{
            ok            = $false
            run_name      = $runName
            stage_reached = "acquire"
            detail        = $detail
            progress_pct  = $pct
            acquisition_record = @{ output_size = $bytes; file_count = $files }
        }
    }
}

function Test-Authorized {
    param([System.Net.HttpListenerRequest]$Request)

    if (-not $expectedToken) {
        return $true
    }
    $provided = $Request.Headers["X-Host-Helper-Token"]
    return ($provided -eq $expectedToken)
}

function Read-RequestJson {
    param([System.Net.HttpListenerRequest]$Request)

    if (-not $Request.HasEntityBody) {
        return @{}
    }
    $reader = New-Object System.IO.StreamReader($Request.InputStream, $Request.ContentEncoding)
    try {
        $raw = $reader.ReadToEnd()
    }
    finally {
        $reader.Close()
    }
    if (-not $raw) {
        return @{}
    }
    return ($raw | ConvertFrom-Json)
}

function Test-FolderHasSegmentNames {
    param(
        [string]$Folder,
        [string[]]$Names
    )

    foreach ($name in $Names) {
        $candidate = Join-Path $Folder $name
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $false
        }
    }
    return $true
}

function Get-SegmentFilenames {
    param([string[]]$Filenames)

    $pattern = '\.(E\d{2}|e\d{2}|001|002|003|004|005|006|007|008|009|dd|raw|aff|aff4|vmdk|vhdx|pas(?:\d+)?|ufd|ufdx|zip)$'
    return @($Filenames | Where-Object { $_ -and ($_ -match $pattern) } | Select-Object -Unique)
}

function Get-RelativeFolderHint {
    param([string[]]$RelativePaths)

    if (-not $RelativePaths -or -not $RelativePaths.Count) {
        return $null
    }

    $dirPaths = @()
    foreach ($rel in $RelativePaths) {
        $parts = ($rel -replace '\\', '/') -split '/' | Where-Object { $_ }
        if ($parts.Count -le 1) {
            $dirPaths += ""
        }
        else {
            $dirPaths += ($parts[0..($parts.Count - 2)] -join '/')
        }
    }

    if (($dirPaths | Select-Object -Unique).Count -eq 1) {
        return $dirPaths[0]
    }
    return $null
}

function Invoke-ListDir {
    param($Path)
    $raw = ("$Path").Trim()
    if (-not $raw) {
        throw "No path specified"
    }
    $norm = $raw -replace '/', '\'
    if (-not (Test-Path -LiteralPath $norm)) {
        throw "Path not found: $norm"
    }
    $item = Get-Item -LiteralPath $norm -Force -ErrorAction Stop
    $folder = if ($item.PSIsContainer) { [string]$item.FullName } else { [string]$item.DirectoryName }
    $files = New-Object System.Collections.Generic.List[hashtable]
    $omitted = New-Object System.Collections.Generic.List[hashtable]
    $names = @()
    try {
        $names = @([System.IO.Directory]::GetFileSystemEntries($folder))
    }
    catch [System.UnauthorizedAccessException] {
        throw "Windows denied access to $folder"
    }
    foreach ($entryPath in $names) {
        $name = [System.IO.Path]::GetFileName($entryPath)
        try {
            $attr = [System.IO.File]::GetAttributes($entryPath)
            $isDir = ($attr -band [System.IO.FileAttributes]::Directory) -ne 0
            $size = $null
            if (-not $isDir) {
                try { $size = [int64](New-Object System.IO.FileInfo($entryPath)).Length } catch { $size = $null }
            }
            $files.Add(@{
                name       = [string]$name
                kind       = $(if ($isDir) { "dir" } else { "file" })
                size_bytes = $size
            }) | Out-Null
        }
        catch [System.UnauthorizedAccessException] {
            $omitted.Add(@{ name = [string]$name; reason = "access_denied"; detail = "Windows denied access" }) | Out-Null
        }
        catch {
            $omitted.Add(@{ name = [string]$name; reason = "unavailable"; detail = $_.Exception.Message }) | Out-Null
        }
    }
    return @{
        ok         = $true
        path       = $folder
        count      = $files.Count
        entries    = $files.ToArray()
        omitted    = $omitted.ToArray()
        error      = $null
    }
}

function Invoke-StageFolder {
    param(
        [string]$Path,
        [string]$JobId
    )
    $raw = ("$Path").Trim()
    if (-not $raw) {
        throw "No path specified"
    }
    $id = ("$JobId").Trim()
    if ($id -notmatch '^[A-Za-z0-9._-]+$') {
        throw "Invalid job_id"
    }
    $norm = $raw -replace '/', '\'
    if (-not (Test-Path -LiteralPath $norm)) {
        throw "Path not found: $norm"
    }
    $item = Get-Item -LiteralPath $norm -Force -ErrorAction Stop
    $src = if ($item.PSIsContainer) { [string]$item.FullName } else { [string]$item.DirectoryName }
    $dest = Join-Path $root "data\uploads\$id\from-path"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    & robocopy $src $dest /E /COPY:DAT /R:1 /W:1 /MT:8 /NFL /NDL /NJH /NJS | Out-Null
    $code = $LASTEXITCODE
    if ($code -ge 8) {
        throw "Failed to copy $src to $dest (robocopy $code)"
    }
    return @{
        ok     = $true
        path   = $src
        dest   = $dest
        job_id = $id
        error  = $null
    }
}

function Invoke-ResolveFolder {
    param(
        [string[]]$Filenames,
        [string]$FolderHint = "",
        [string[]]$RelativePaths = @()
    )

    $names = Get-SegmentFilenames $Filenames
    if (-not $names.Count) {
        throw "No disk or mobile segment files (.E01, .pas, .ufd, ...) in the selection"
    }

    # Fast path: absolute Windows path (G:\folder\...) - never scan whole disks.
    $absoluteHints = New-Object System.Collections.Generic.List[string]
    foreach ($raw in (@($FolderHint) + @($RelativePaths))) {
        if (-not $raw) { continue }
        $t = [string]$raw.Trim()
        if ($t -match '^[A-Za-z]:[\\/]') {
            $norm = $t -replace '/', '\'
            if (Test-Path -LiteralPath $norm -PathType Leaf) {
                $norm = Split-Path -LiteralPath $norm -Parent
            }
            if ($norm -and -not $absoluteHints.Contains($norm)) {
                [void]$absoluteHints.Add($norm)
            }
        }
    }
    foreach ($abs in $absoluteHints) {
        if ((Test-Path -LiteralPath $abs -PathType Container) -and (Test-FolderHasSegmentNames $abs $names)) {
            return @{
                ok            = $true
                path          = $abs
                segment_count = $names.Count
                error         = $null
            }
        }
    }

    $hints = New-Object System.Collections.Generic.List[string]
    $relHint = Get-RelativeFolderHint $RelativePaths
    if ($relHint) { [void]$hints.Add(($relHint -replace '/', '\')) }
    if ($FolderHint) {
        $clean = $FolderHint.Trim().Trim('\', '/')
        if ($clean -match '^[A-Za-z]:[\\/]?(.*)$') {
            $clean = $Matches[1]
        }
        if ($clean -and -not $hints.Contains($clean)) {
            [void]$hints.Add($clean)
        }
    }

    $drives = @(Get-WorkstationDriveLetters | ForEach-Object { "${_}:\" })
    if (-not $drives.Count) {
        $drives = @(Get-PSDrive -PSProvider FileSystem |
            Where-Object { $_.Name -match '^[A-Z]$' } |
            ForEach-Object { "$($_.Name):\" })
    }

    # Prefer exact Join-Path candidates only (no deep recurse).
    foreach ($hint in $hints) {
        if (-not $hint) { continue }
        $hintParts = @($hint -split '[\\/]' | Where-Object { $_ })
        foreach ($drive in $drives) {
            $candidates = New-Object System.Collections.Generic.List[string]
            [void]$candidates.Add((Join-Path $drive $hint))
            for ($i = 0; $i -lt [Math]::Min($hintParts.Count, 4); $i++) {
                [void]$candidates.Add((Join-Path $drive ($hintParts[$i..($hintParts.Count - 1)] -join '\')))
            }
            foreach ($candidate in ($candidates | Select-Object -Unique)) {
                if ((Test-Path -LiteralPath $candidate -PathType Container) -and (Test-FolderHasSegmentNames $candidate $names)) {
                    return @{
                        ok            = $true
                        path          = $candidate
                        segment_count = $names.Count
                        error         = $null
                    }
                }
            }
        }
    }

    # Name search on non-system drives (skip C:). Depth 8 covers paths like
    # D:\all\Fotrensics_Data\DISK2\DataExtration\SegerEx-1 (was depth 3 → never found).
    $baseName = $null
    if ($hints.Count) {
        $baseName = Split-Path ($hints[0]) -Leaf
    }
    $searchDepth = 8
    if ($baseName) {
        $searchDrives = @($drives | Where-Object { $_ -notmatch '^[Cc]:' })
        foreach ($drive in $searchDrives) {
            try {
                $matches = Get-ChildItem -LiteralPath $drive -Filter $baseName -Directory -Recurse -Depth $searchDepth -ErrorAction SilentlyContinue |
                    Select-Object -First 40
                foreach ($match in $matches) {
                    if (Test-FolderHasSegmentNames $match.FullName $names) {
                        return @{
                            ok            = $true
                            path          = $match.FullName
                            segment_count = $names.Count
                            error         = $null
                        }
                    }
                }
            }
            catch { }
        }
    }

    # Last resort: look for first segment filename only, non-C: drives.
    $anchor = $names | Select-Object -First 1
    if ($anchor) {
        $searchDrives = @($drives | Where-Object { $_ -notmatch '^[Cc]:' })
        foreach ($drive in $searchDrives) {
            try {
                $hits = Get-ChildItem -LiteralPath $drive -Filter $anchor -File -Recurse -Depth $searchDepth -ErrorAction SilentlyContinue |
                    Select-Object -First 20
                foreach ($hit in $hits) {
                    if (Test-FolderHasSegmentNames $hit.DirectoryName $names) {
                        return @{
                            ok            = $true
                            path          = $hit.DirectoryName
                            segment_count = $names.Count
                            error         = $null
                        }
                    }
                }
            }
            catch { }
        }
    }

    throw "Could not find a workstation folder containing $($names.Count) selected segment file(s). Choose that folder again from the drive list."
}

function Get-WorkstationDriveLetters {
    $volumes = Get-WorkstationDriveVolumes
    return @($volumes | ForEach-Object { $_.letter } | Sort-Object -Unique)
}

function Get-ComposeDriveLettersFromFile {
    param([string]$Path)
    $letters = @()
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return @() }
    foreach ($line in @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
        if (("$line") -match 'target:\s*/host/([a-zA-Z])\s*$') {
            $letters += $Matches[1].ToUpper()
        }
    }
    return @($letters | Sort-Object -Unique)
}

function Start-DrivePresenceWatcher {
    # V50: drive discovery is passive.  Do not recreate Docker services merely
    # because a Windows drive letter appeared, disappeared, or changed media.
    # The folder picker lists directly through HostDrive.  Once the examiner
    # chooses a directory, /api/hostdrive/ensure-path mounts that exact path
    # read-only immediately before extraction starts.
    $script:LastSeenDriveKey = ((Get-WorkstationDriveLetters) -join ',')

    $timer = New-Object System.Timers.Timer
    $timer.Interval = 8000
    $timer.AutoReset = $true
    $null = Register-ObjectEvent -InputObject $timer -EventName Elapsed -SourceIdentifier "AetherisDriveWatch" -MessageData @{ Root = $root } -Action {
        try {
            $letters = New-Object System.Collections.Generic.List[string]
            Get-Volume -ErrorAction SilentlyContinue |
                Where-Object { $_.DriveLetter -and "$($_.DriveLetter)" -match '^[A-Z]$' } |
                ForEach-Object { [void]$letters.Add("$($_.DriveLetter)".ToUpper()) }
            Get-Partition -ErrorAction SilentlyContinue |
                Where-Object { $_.DriveLetter -and "$($_.DriveLetter)" -match '^[A-Z]$' } |
                ForEach-Object { [void]$letters.Add("$($_.DriveLetter)".ToUpper()) }
            Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
                Where-Object { $_.DeviceID -match '^[A-Z]:$' } |
                ForEach-Object { [void]$letters.Add($_.DeviceID.Substring(0, 1).ToUpper()) }
            foreach ($code in 65..90) {
                $L = [string][char]$code
                if ($letters -contains $L) { continue }
                if (Test-Path -LiteralPath "${L}:\") { [void]$letters.Add($L) }
            }
            $key = (($letters | Sort-Object -Unique) -join ',')
            $stateDir = Join-Path $env:LOCALAPPDATA "Aetheris"
            if (-not (Test-Path -LiteralPath $stateDir)) {
                New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
            }
            $stateFile = Join-Path $stateDir "helper-drive-letters.txt"
            $prev = if (Test-Path -LiteralPath $stateFile) { (Get-Content -LiteralPath $stateFile -Raw).Trim() } else { '' }
            if ($key -ne $prev) {
                Write-Host "[HostDrive] Drive set changed: $prev -> $key (no Docker restart; mount deferred until extraction)."
                Set-Content -LiteralPath $stateFile -Value $key -Encoding UTF8
            }
        }
        catch { }
    }
    $timer.Enabled = $true
    $script:DriveWatchTimer = $timer
}

function Resolve-MobileOsHint {
    param(
        [string]$FriendlyName = "",
        [string]$InstanceId = ""
    )
    $blob = "$FriendlyName $InstanceId"
    if ($blob -match '(?i)VID_05AC|iPhone|iPad|iPod|Apple Mobile Device|Apple iPad') {
        return "ios"
    }
    # Google, Samsung, Xiaomi, Huawei, OnePlus, HTC, Motorola, MediaTek, Oppo, Vivo,
    # ZTE, Alcatel, Qualcomm (many cheap/old Androids), Sony, LG, Asus, Lenovo, Zebra.
    $androidVid = '(?i)VID_(18D1|04E8|2717|2A45|2A47|0BB4|22B8|12D1|2A70|0E8D|19D2|22D9|29A9|1BBB|05C6|0A9D|2B4C|0FCE|1004|0B05|17EF|0E79|2D95)'
    $androidName = '(?i)Android|Samsung|Galaxy|Xiaomi|Redmi|POCO|Huawei|Honor|OnePlus|Pixel|Motorola|Moto|Nokia|Oppo|Vivo|Realme|Tecno|Infinix|Itel|Nothing|Asus|Sony|Xperia|LG |Lenovo|ZTE|Alcatel|TCL|Meizu|Fairphone|Blackview|Umidigi|Doogee|Lava|Micromax|MTP'
    if ($blob -match $androidVid -or $blob -match $androidName) {
        return "android"
    }
    return "other"
}

function Get-IosBackupRoots {
    $candidates = @(
        (Join-Path $env:USERPROFILE "Apple\MobileSync\Backup"),
        (Join-Path $env:APPDATA "Apple Computer\MobileSync\Backup"),
        (Join-Path $env:USERPROFILE "AppData\Roaming\Apple Computer\MobileSync\Backup")
    )
    $roots = @()
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path -PathType Container)) {
            $roots += $path
        }
    }
    return @($roots | Select-Object -Unique)
}


function Get-PnpClassSafe {
    param([string]$Class, [int]$TimeoutSec = 4)
    # Get-PnpDevice can hang forever on a wedged phone USB stack. Bound it.
    try {
        $job = Start-Job -ScriptBlock {
            param($c)
            @(Get-PnpDevice -PresentOnly -Class $c -ErrorAction SilentlyContinue |
                Where-Object { $_.Status -eq "OK" } |
                ForEach-Object {
                    [pscustomobject]@{
                        FriendlyName = [string]$_.FriendlyName
                        Class        = [string]$_.Class
                        InstanceId   = [string]$_.InstanceId
                    }
                })
        } -ArgumentList $Class
        if (Wait-Job $job -Timeout $TimeoutSec) {
            $rows = @(Receive-Job $job)
            Remove-Job $job -Force -ErrorAction SilentlyContinue
            return $rows
        }
        Stop-Job $job -ErrorAction SilentlyContinue
        Remove-Job $job -Force -ErrorAction SilentlyContinue
        return @()
    }
    catch {
        return @()
    }
}

function Get-PcscSimStatus {
    $readers = New-Object System.Collections.Generic.List[string]
    $winscard = Join-Path $env:SystemRoot "System32\winscard.dll"
    $svc = Get-Service -Name "SCardSvr" -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $winscard) -and $null -eq $svc) {
        return @{ available = $false; readers = @(); reason = "Windows PC/SC (winscard / SCardSvr) is not present on this PC" }
    }
    if ($null -ne $svc -and $svc.Status -ne "Running") {
        try { Start-Service -Name "SCardSvr" -ErrorAction Stop } catch {}
        try { $svc.Refresh() } catch {}
    }
    # Do not call Get-PnpDevice — it can hang the helper request queue.
    $svcRunning = $null -ne $svc -and $svc.Status -eq "Running"
    if ($readers.Count -gt 0) { $reason = "PC/SC ready on examiner host ($($readers.Count) reader(s))" }
    elseif ($svcRunning) { $reason = "PC/SC (SCardSvr) ready - plug in a SIM/USIM reader to detect cards" }
    else { $reason = "PC/SC tooling present (winscard). Start SCardSvr and attach a SIM reader." }
    return @{ available = $true; readers = @($readers); reason = $reason }
}
function Get-StartedUsbInstanceIds {
    $ids = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $script:LastConnectedUsbLines = @()
    $tmp = Join-Path $env:TEMP ("aetheris_pnputil_" + [guid]::NewGuid().ToString("N") + ".txt")
    $err = $tmp + ".err"
    $pnputil = Join-Path $env:SystemRoot "System32\pnputil.exe"
    $pnputilOk = $false
    $finished = $false
    try {
        if (Test-Path -LiteralPath $pnputil) {
            $proc = Start-Process -FilePath $pnputil -ArgumentList @("/enum-devices", "/connected", "/ids") `
                -PassThru -WindowStyle Hidden -RedirectStandardOutput $tmp -RedirectStandardError $err
            # Never kill pnputil. Aborting enumeration resets the phone USB interfaces.
            $finished = $false
            try { $finished = $proc.WaitForExit(20000) } catch { $finished = $false }
            if ($finished -and $proc.ExitCode -eq 0 -and (Test-Path -LiteralPath $tmp)) {
                $script:LastConnectedUsbLines = @(Get-Content -LiteralPath $tmp -ErrorAction SilentlyContinue)
                foreach ($line in $script:LastConnectedUsbLines) {
                    if ($line -match '(?i)Instance ID:\s+(\S+)') {
                        $id = [string]$Matches[1]
                        if ($id -match '(?i)USB\\VID_') { [void]$ids.Add($id) }
                    }
                }
                $pnputilOk = $true
            }
        }
    } catch {}
    finally {
        if ($finished) {
            Remove-Item -LiteralPath $tmp, $err -Force -ErrorAction SilentlyContinue
        }
    }
    if ($pnputilOk) { return $ids }
    $svcRoot = "HKLM:\SYSTEM\CurrentControlSet\Services"
    try {
        Get-ChildItem -LiteralPath $svcRoot -ErrorAction SilentlyContinue | ForEach-Object {
            $enumPath = Join-Path $_.PSPath "Enum"
            if (-not (Test-Path -LiteralPath $enumPath)) { return }
            $enum = Get-ItemProperty -LiteralPath $enumPath -ErrorAction SilentlyContinue
            if (-not $enum) { return }
            $count = 0
            try { $count = [int]$enum.Count } catch { $count = 0 }
            if ($count -le 0) { return }
            for ($i = 0; $i -lt $count; $i++) {
                $id = [string]$enum."$i"
                if ($id -match '(?i)USB\\VID_') { [void]$ids.Add($id) }
            }
        }
    } catch {}
    $script:LastConnectedUsbLines = @(Get-RegistryUsbLines -Ids $ids)
    return $ids
}

function Get-RegistryUsbLines {
    # V1.31.6: pnputil-style lines (Instance ID / Device Description / Status / Compatible IDs) read from
    # the registry for the USB devices Windows is running, so USB-debugging phones are still recognised
    # (interface class FF/42/01) when pnputil is not used. Reading the registry never touches USB.
    param($Ids)
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($id in @($Ids)) {
        if (-not $id) { continue }
        $props = Get-ItemProperty -LiteralPath ("HKLM:\SYSTEM\CurrentControlSet\Enum\" + $id) -ErrorAction SilentlyContinue
        $desc = ""
        if ($props -and $props.PSObject.Properties['DeviceDesc']) { $desc = [string]$props.DeviceDesc }
        if ($desc -match ';(.+)$') { $desc = $Matches[1] }
        $lines.Add("Instance ID:                $id")
        $lines.Add("Device Description:         $desc")
        if ($props -and $props.PSObject.Properties['Mfg']) {
            $mfg = [string]$props.Mfg
            if ($mfg -match ';(.+)$') { $mfg = $Matches[1] }
            $lines.Add("Manufacturer Name:          $mfg")
        }
        $lines.Add("Status:                     Started")
        if ($props -and $props.PSObject.Properties['CompatibleIDs']) {
            foreach ($c in @($props.CompatibleIDs)) { if ($c) { $lines.Add("Compatible IDs:             $c") } }
        }
    }
    return [string[]]$lines.ToArray()
}

function Test-WpdIdCurrentlyAttached {
    param([string]$WpdId, $StartedIds)
    if (-not $WpdId -or $null -eq $StartedIds) { return $false }
    $norm = ($WpdId -replace '#', '\' -replace '/', '\').ToUpperInvariant()
    $vid = $null; $prodId = $null; $serial = $null
    if ($norm -match 'VID_([0-9A-F]{4})') { $vid = $Matches[1] }
    if ($norm -match 'PID_([0-9A-F]{4})') { $prodId = $Matches[1] }
    if ($norm -match 'VID_[0-9A-F]{4}&PID_[0-9A-F]{4}[^\\]*\\([^\\&]+)') { $serial = $Matches[1] }
    if (-not $vid -or -not $prodId) { return $false }
    foreach ($started in $StartedIds) {
        $s = ([string]$started).ToUpperInvariant()
        if ($s -notmatch ("VID_" + $vid) -or $s -notmatch ("PID_" + $prodId)) { continue }
        if ($serial -and $s -match [regex]::Escape($serial)) { return $true }
        if (-not $serial) { return $true }
    }
    return $false
}

function Get-ConnectedAdbPhones {
    param([string[]]$Lines)
    # Android USB debugging uses interface class FF / subclass 42 / protocol 01.
    # That phone has no Windows Portable Device entry until File transfer (MTP) is selected,
    # so a WPD-only scan reports "no phone" while the handset is already on USB.
    $phones = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    $id = ""
    $desc = ""
    $status = ""
    $adb = $false
    $commit = {
        if ($adb -and ((-not $status) -or $status -eq "Started") -and $id -match '(?i)^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&MI_[0-9A-F]+)?\\(.+)$') {
            $vid = $Matches[1].ToUpperInvariant()
            $productId = $Matches[2].ToUpperInvariant()
            $serial = $Matches[3]
            # Composite ADB interfaces are USB\VID_..&PID_..&MI_01\7&node&0&0001. That tail is not the
            # phone serial. Keep the instance id; the read matches the one phone adb actually lists.
            if ($serial -match '&') { $serial = $id }
            $key = "android|$vid|$productId"
            if (-not $seen.ContainsKey($key)) {
                $seen[$key] = $true
                $name = [string]$desc
                if (-not $name -or $name -match '(?i)^(USB Composite Device|USB Device|WinUsb Device|ADB Interface|ADB Device|Android ADB Interface|Android Composite ADB Interface)$') {
                    $name = "Android phone"
                }
                $phones.Add(@{
                    id               = $serial
                    name             = $name
                    device_class     = "ADB"
                    connection       = "adb"
                    os_hint          = "android"
                    instance_id      = $id
                    has_drive_letter = $false
                    live_acquire     = $true
                    capability_label = "SUPPORTED_DIRECT_LOGICAL"
                    guidance         = "Phone is connected with USB debugging. Unlock the screen and tap Allow if the phone asks to trust this computer."
                    ios_backup_roots = @()
                })
            }
        }
    }
    foreach ($line in @($Lines)) {
        $text = [string]$line
        if ($text -match '(?i)Instance ID:\s+(\S+)') {
            . $commit
            $id = [string]$Matches[1]
            $desc = ""
            $status = ""
            $adb = $false
            continue
        }
        if ($text -match '(?i)Device Description:\s+(.+?)\s*$') { $desc = [string]$Matches[1].Trim(); continue }
        if ($text -match '(?i)Status:\s+(\S+)') { $status = [string]$Matches[1]; continue }
        if ($text -match '(?i)SubClass_42&Prot_01|MS_COMP_ADB') { $adb = $true }
    }
    . $commit
    return [object[]]$phones.ToArray()
}

function Get-AndroidUsbStates {
    # V1.31.7: what Windows shows of Android phones on USB, from pnputil-style lines (pnputil or registry).
    #   MtpKeys - VID|PID of phones with a running File transfer (MTP) interface, whatever Windows calls them
    #             (many phones are listed only by a model code such as SM-A546E or CPH2451).
    #   Ready   - phones with a running File transfer interface that Windows' portable-device list does not
    #             show (the collection finds them by their USB vendor and product ID).
    #   Rows    - phones that are plugged in but not usable yet (charging only, or no Windows driver for their
    #             interface), with what to do on the phone. Before, such a phone was simply not listed.
    param([string[]]$Lines)
    $phoneVendors = @('18D1', '2717', '2A70', '22D9', '2D95', '22B8', '339B', '2A45', '2E04', '29A9', '2B4C')
    $maybeVendors = @('04E8', '0FCE', '1004', '0B05', '0E8D', '05C6', '12D1', '19D2', '0BB4')
    $notPhoneWords = '(?i)modem|network|broadband|mobile connect|card reader|keyboard|mouse|headset|webcam|printer|bluetooth|mass storage|ssd|flash drive'
    $phoneWords = '(?i)android|phone|galaxy|pixel|redmi|xiaomi|poco|oneplus|oppo|vivo|realme|iqoo|moto|nokia|honor|huawei|nothing|infinix|tecno|itel|xperia|mobile'
    $genericDesc = '(?i)^(USB Composite Device|USB Device|WinUsb Device|ADB Interface|ADB Device|Android ADB Interface|Android Composite ADB Interface|MTP USB Device|USB Input Device|Generic USB Hub|USB Mass Storage Device)$'
    # Names used only when nothing better is shown: the File transfer interface carries the phone's own name.
    $fallbackDesc = '(?i)Composite Device$|ADB Interface$|Modem$'
    $groups = @{}
    $block = $null
    $flush = {
        if ($block -and $block.Id -match '(?i)^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(&MI_[0-9A-F]{2})?\\(.+)$') {
            $vid = $Matches[1].ToUpperInvariant()
            $prodId = $Matches[2].ToUpperInvariant()
            $iface = [bool]$Matches[3]
            $tail = [string]$Matches[4]
            if ($vid -ne '05AC') {
                $key = "android|$vid|$prodId"
                if (-not $groups.ContainsKey($key)) {
                    $groups[$key] = @{ Vid = $vid; ProdId = $prodId; Serial = ''; Instance = ''; Desc = ''; Phone = $false; AdbOk = $false; AdbBad = $false; MtpOk = $false; MtpBad = $false; MsMtp = $false; MtpDesc = ''; FallbackDesc = '' }
                }
                $g = $groups[$key]
                $ids = [string]$block.Ids
                $started = ((-not $block.Status) -or $block.Status -eq 'Started')
                $isAdb = ($ids -match '(?i)SubClass_42&Prot_01|MS_COMP_ADB')
                $isMtp = (($ids -match '(?i)MS_COMP_MTP') -or ($ids -match '(?i)Class_06&SubClass_01&Prot_01'))
                if ($isAdb) { if ($started) { $g.AdbOk = $true } else { $g.AdbBad = $true } }
                if ($isMtp) { if ($started) { $g.MtpOk = $true } else { $g.MtpBad = $true } }
                if ($ids -match '(?i)MS_COMP_MTP') { $g.MsMtp = $true }
                if ((-not $iface) -and $tail -notmatch '&') { $g.Serial = $tail; $g.Instance = $block.Id }
                if (-not $g.Instance) { $g.Instance = $block.Id }
                $words = ([string]$block.Desc + ' ' + [string]$block.Mfr)
                if ($isAdb -or ($phoneVendors -contains $vid) -or ((($maybeVendors -contains $vid) -or $isMtp) -and $words -match $phoneWords -and $words -notmatch $notPhoneWords)) { $g.Phone = $true }
                if ($block.Desc -and $block.Desc -notmatch $genericDesc) {
                    if ($isMtp -and -not $g.MtpDesc) { $g.MtpDesc = [string]$block.Desc }
                    if ($block.Desc -match $fallbackDesc) { if (-not $g.FallbackDesc) { $g.FallbackDesc = [string]$block.Desc } }
                    elseif (-not $g.Desc) { $g.Desc = [string]$block.Desc }
                }
            }
        }
    }
    foreach ($line in @($Lines)) {
        $text = [string]$line
        if ($text -match '(?i)^\s*Instance ID:\s+(\S+)') {
            $newId = [string]$Matches[1]   # read before the flush, which runs its own matches
            . $flush
            $block = @{ Id = $newId; Desc = ''; Mfr = ''; Status = ''; Ids = '' }
            continue
        }
        if (-not $block) { continue }
        if ($text -match '(?i)^\s*Device Description:\s+(.+?)\s*$') { $block.Desc = [string]$Matches[1]; continue }
        if ($text -match '(?i)^\s*Manufacturer Name:\s+(.+?)\s*$') { $block.Mfr = [string]$Matches[1]; continue }
        if ($text -match '(?i)^\s*Status:\s+(\S+)') { $block.Status = [string]$Matches[1]; continue }
        $block.Ids = [string]$block.Ids + ' ' + $text.Trim()
    }
    . $flush
    $mtpKeys = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $rows = @()
    $ready = @()
    foreach ($key in @($groups.Keys)) {
        $g = $groups[$key]
        # A camera uses the same picture-transfer class; Android's File transfer also says MS_COMP_MTP.
        if ($g.MtpOk -and ($g.Phone -or $g.MsMtp)) {
            [void]$mtpKeys.Add($g.Vid + '|' + $g.ProdId)
            $ready += @{
                id               = $g.Instance
                name             = $(if ($g.MtpDesc) { $g.MtpDesc } elseif ($g.Desc) { $g.Desc } elseif ($g.FallbackDesc) { $g.FallbackDesc } else { 'Android phone (File transfer)' })
                device_class     = 'WPD'
                connection       = 'mtp'
                os_hint          = 'android'
                instance_id      = $g.Instance
                has_drive_letter = $false
                live_acquire     = $true
                capability_label = 'SUPPORTED_DIRECT_LOGICAL'
                guidance         = 'Connected phone detected (File transfer). Use Start extraction.'
                ios_backup_roots = @()
            }
        }
        if ((-not $g.Phone) -or $g.AdbOk -or $g.MtpOk) { continue }
        $state = 'charging_only'
        $guidance = 'Connected for charging only. Unlock the phone, open the USB notification on it and choose File transfer (for a deeper collection also turn on USB debugging and tap Allow), then Rescan.'
        if ($g.AdbBad -or $g.MtpBad) {
            $state = 'driver_missing'
            $guidance = 'Windows has no working driver for this phone''s USB interface (Device Manager shows it with a warning). On the phone choose USB: File transfer, which needs no extra driver, or install the phone maker''s USB driver, then Rescan.'
        }
        $rows += @{
            id               = $(if ($g.Serial) { $g.Serial } else { $g.Instance })
            name             = $(if ($g.Desc) { $g.Desc } elseif ($g.FallbackDesc) { $g.FallbackDesc } else { 'Android phone' })
            device_class     = 'USB'
            connection       = 'usb'
            os_hint          = 'android'
            instance_id      = $g.Instance
            has_drive_letter = $false
            live_acquire     = $false
            state            = $state
            capability_label = 'NEEDS_ACTION'
            guidance         = $guidance
            ios_backup_roots = @()
        }
    }
    return @{ MtpKeys = $mtpKeys; Ready = $ready; Rows = $rows }
}

function Get-PresentWpdPhones {
    $started = Get-StartedUsbInstanceIds
    $usbStates = Get-AndroidUsbStates -Lines $script:LastConnectedUsbLines
    $list = @()
    $seen = @{}
    foreach ($regRoot in @(
        "HKLM:\SOFTWARE\Microsoft\Windows Portable Devices\Devices",
        "HKCU:\SOFTWARE\Microsoft\Windows Portable Devices\Devices"
    )) {
        if (-not (Test-Path -LiteralPath $regRoot)) { continue }
        Get-ChildItem -LiteralPath $regRoot -ErrorAction SilentlyContinue | ForEach-Object {
            $props = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            $name = [string]$props.FriendlyName
            $id = [string]$_.PSChildName
            if (-not $name) { $name = $id }
            if (-not $id) { return }
            if ($name -match '(?i)^(Local Disk|Windows|Network|CD Drive|DVD|Removable Disk|New Volume|CCCOMA_|RUFUS_)') { return }
            $mtpPhone = $false
            if ($id -match '(?i)VID_([0-9A-F]{4}).*?PID_([0-9A-F]{4})') { $mtpPhone = $usbStates.MtpKeys.Contains($Matches[1] + '|' + $Matches[2]) }
            $looksPhone = ($name -match '(?i)iPhone|iPad|iPod|Apple|Android|Samsung|Galaxy|Xiaomi|Redmi|Huawei|Honor|OnePlus|Pixel|Oppo|Vivo|Tecno|Infinix|Motorola|Nokia|Realme|Nothing|Phone') -or
                ($id -match '(?i)VID_(05AC|18D1|04E8|2717|2A45|0E8D|2A70|19D2|22D9|29A9|05C6|12D1|22B8|0BB4|0FCE|1004|0B05|2B4C|2D95|339B|2E04)') -or $mtpPhone
            if (-not $looksPhone) { return }
            if (-not (Test-WpdIdCurrentlyAttached -WpdId $id -StartedIds $started)) { return }
            $os = "android"
            if ($name -match '(?i)iPhone|iPad|iPod|Apple' -or $id -match '(?i)VID_05AC') { $os = "ios" }
            $key = ($name + "|" + $os).ToLowerInvariant()
            if ($id -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
                $key = $os + "|" + $Matches[1].ToUpperInvariant() + "|" + $Matches[2].ToUpperInvariant()
            }
            if ($seen.ContainsKey($key)) { return }
            $seen[$key] = $true
            $list += @{
                id               = $id
                name             = $name
                device_class     = "WPD"
                connection       = "mtp"
                os_hint          = $os
                instance_id      = $id
                has_drive_letter = $false
                live_acquire     = $true
                capability_label = "SUPPORTED_DIRECT_LOGICAL"
                guidance         = "Connected phone detected. Use Start extraction."
                ios_backup_roots = @()
            }
        }
    }
    foreach ($adbPhone in @(Get-ConnectedAdbPhones -Lines $script:LastConnectedUsbLines)) {
        $key = ""
        if ([string]$adbPhone.instance_id -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
            $key = "android|" + $Matches[1].ToUpperInvariant() + "|" + $Matches[2].ToUpperInvariant()
        }
        if (-not $key) { continue }
        # File transfer and USB debugging share one VID/PID. Keep the debugging entry so a read
        # is not aimed at the MTP-only record.
        if ($seen.ContainsKey($key)) {
            $list = @($list | Where-Object {
                $rowKey = ""
                if ([string]$_.instance_id -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
                    $rowKey = "android|" + $Matches[1].ToUpperInvariant() + "|" + $Matches[2].ToUpperInvariant()
                }
                $rowKey -ne $key
            })
        }
        $seen[$key] = $true
        $list += $adbPhone
    }
    foreach ($row in @(@($usbStates.Ready) + @($usbStates.Rows))) {
        $rowKey = ""
        if ([string]$row.instance_id -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
            $rowKey = "android|" + $Matches[1].ToUpperInvariant() + "|" + $Matches[2].ToUpperInvariant()
        }
        if (-not $rowKey -or $seen.ContainsKey($rowKey)) { continue }
        $seen[$rowKey] = $true
        $list += $row
    }
    return $list
}

function Write-MobileDeviceCaches {
    $list = @(Get-PresentWpdPhones)
    $json = if ($list.Count -eq 0) { "[]" } elseif ($list.Count -eq 1) { "[" + ($list[0] | ConvertTo-Json -Compress -Depth 6) + "]" } else { ($list | ConvertTo-Json -Compress -Depth 6) }
    [System.IO.File]::WriteAllText($script:DeviceCacheFile, $json)
    $acq = @()
    foreach ($d in $list) {
        $os = [string]$d.os_hint
        $conn = [string]$d.connection
        $adapter = "android_mtp"
        $status = "wpd"
        if ($os -eq "ios") { $adapter = "ios_lockdown" }
        elseif ($conn -eq "adb") { $adapter = "android_adb"; $status = "adb" }
        elseif ($conn -eq "usb") { $adapter = "android_usb"; $status = [string]$d.state }
        $acq += @{
            adapter     = $adapter
            device_id   = [string]$d.id
            label       = [string]$d.name
            status      = $status
            os_family   = $(if ($os) { $os } else { "android" })
            instance_id = [string]$d.instance_id
            connection  = $conn
            guidance    = [string]$d.guidance
            state       = [string]$d.state
        }
    }
    $acqBody = @{ ok = $true; devices = $acq; warnings = @(); count = $acq.Count; scanned_utc = (Get-Date).ToUniversalTime().ToString("o"); source = "startup" }
    [System.IO.File]::WriteAllText($script:AcqDevicesCacheFile, ($acqBody | ConvertTo-Json -Compress -Depth 6))
    $script:DeviceCache = $list
    $script:DeviceCacheAt = Get-Date
}

function Read-MobileDeviceCacheFile {
    $file = $script:DeviceCacheFile
    if (-not $file -or -not (Test-Path -LiteralPath $file)) { return @($script:DeviceCache) }
    try {
        $raw = Get-Content -LiteralPath $file -Raw -ErrorAction Stop
        if (-not $raw) { return @($script:DeviceCache) }
        $obj = $raw | ConvertFrom-Json
        if ($null -eq $obj) { return @($script:DeviceCache) }
        $rows = @($obj)
        $script:DeviceCache = $rows
        $script:DeviceCacheAt = Get-Date
        return $rows
    } catch {
        return @($script:DeviceCache)
    }
}

function Start-MobileDeviceRefresh {
    if ($script:DeviceRefreshJob -and $script:DeviceRefreshJob.State -eq "Running") { return }
    try {
        if ($script:DeviceRefreshJob) {
            Remove-Job $script:DeviceRefreshJob -Force -ErrorAction SilentlyContinue
        }
    } catch {}
    $script:DeviceRefreshJob = Start-Job -Name "aetheris-mobile-refresh" -ScriptBlock {
        param($CacheFile, $AcqFile)
        $script:PnputilProc = $null
        $script:PnputilOut = Join-Path $env:TEMP "aetheris_mobile_pnputil.txt"
        $script:PnputilErr = Join-Path $env:TEMP "aetheris_mobile_pnputil.err"
        $script:EnumFresh = $false
        function Read-PnputilUsbIds {
            param([string]$Path)
            $ids = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
            $script:LastConnectedUsbLines = @()
            if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $ids }
            $script:LastConnectedUsbLines = @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)
            foreach ($line in $script:LastConnectedUsbLines) {
                if ($line -match '(?i)Instance ID:\s+(\S+)') {
                    $id = [string]$Matches[1]
                    if ($id -match '(?i)USB\\VID_') { [void]$ids.Add($id) }
                }
            }
            return $ids
        }
        function Get-RegistryUsbLines {
            param($Ids)
            $lines = New-Object System.Collections.Generic.List[string]
            foreach ($id in @($Ids)) {
                if (-not $id) { continue }
                $props = Get-ItemProperty -LiteralPath ("HKLM:\SYSTEM\CurrentControlSet\Enum\" + $id) -ErrorAction SilentlyContinue
                $desc = ""
                if ($props -and $props.PSObject.Properties['DeviceDesc']) { $desc = [string]$props.DeviceDesc }
                if ($desc -match ';(.+)$') { $desc = $Matches[1] }
                $lines.Add("Instance ID:                $id")
                $lines.Add("Device Description:         $desc")
                if ($props -and $props.PSObject.Properties['Mfg']) {
                    $mfg = [string]$props.Mfg
                    if ($mfg -match ';(.+)$') { $mfg = $Matches[1] }
                    $lines.Add("Manufacturer Name:          $mfg")
                }
                $lines.Add("Status:                     Started")
                if ($props -and $props.PSObject.Properties['CompatibleIDs']) {
                    foreach ($c in @($props.CompatibleIDs)) { if ($c) { $lines.Add("Compatible IDs:             $c") } }
                }
            }
            return [string[]]$lines.ToArray()
        }
        function Get-RegistryStartedUsbIds {
            # The USB devices Windows is running, from each driver's Enum key. No USB traffic at all.
            $ids = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
            Get-ChildItem -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services" -ErrorAction SilentlyContinue | ForEach-Object {
                $enumPath = Join-Path $_.PSPath "Enum"
                if (-not (Test-Path -LiteralPath $enumPath)) { return }
                $enum = Get-ItemProperty -LiteralPath $enumPath -ErrorAction SilentlyContinue
                if (-not $enum) { return }
                $count = 0
                try { $count = [int]$enum.Count } catch { $count = 0 }
                for ($i = 0; $i -lt $count; $i++) {
                    $id = [string]$enum."$i"
                    if ($id -match '(?i)USB\\VID_') { [void]$ids.Add($id) }
                }
            }
            $script:LastConnectedUsbLines = @(Get-RegistryUsbLines -Ids $ids)
            return $ids
        }
        function Get-StartedUsbInstanceIds {
            param([switch]$RegistryOnly)
            $script:EnumFresh = $false
            if ($RegistryOnly) {
                $ids = Get-RegistryStartedUsbIds
                $script:EnumFresh = $true
                return $ids
            }
            if ($script:PnputilProc -and -not $script:PnputilProc.HasExited) {
                return (New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase))
            }
            if ($script:PnputilProc -and $script:PnputilProc.HasExited) {
                $done = $script:PnputilProc
                $script:PnputilProc = $null
                $ids = Read-PnputilUsbIds -Path $script:PnputilOut
                Remove-Item -LiteralPath $script:PnputilOut, $script:PnputilErr -Force -ErrorAction SilentlyContinue
                if ($done.ExitCode -eq 0) {
                    $script:EnumFresh = $true
                    return $ids
                }
            }
            $pnputil = Join-Path $env:SystemRoot "System32\pnputil.exe"
            if (Test-Path -LiteralPath $pnputil) {
                try {
                    $proc = Start-Process -FilePath $pnputil -ArgumentList @("/enum-devices", "/connected", "/ids") `
                        -PassThru -WindowStyle Hidden -RedirectStandardOutput $script:PnputilOut -RedirectStandardError $script:PnputilErr
                    $finished = $false
                    try { $finished = $proc.WaitForExit(20000) } catch { $finished = $false }
                    if (-not $finished) {
                        # Leave it running. Killing pnputil mid-enum resets Android and iPhone USB.
                        $script:PnputilProc = $proc
                        return (New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase))
                    }
                    $ids = Read-PnputilUsbIds -Path $script:PnputilOut
                    Remove-Item -LiteralPath $script:PnputilOut, $script:PnputilErr -Force -ErrorAction SilentlyContinue
                    if ($proc.ExitCode -eq 0) {
                        $script:EnumFresh = $true
                        return $ids
                    }
                } catch {}
            }
            $ids = Get-RegistryStartedUsbIds
            $script:EnumFresh = $true
            return $ids
        }
        function Get-ConnectedAdbPhones {
            param([string[]]$Lines)
            $phones = New-Object System.Collections.Generic.List[object]
            $seenAdb = @{}
            $id = ""
            $desc = ""
            $status = ""
            $adb = $false
            $commit = {
                if ($adb -and ((-not $status) -or $status -eq "Started") -and $id -match '(?i)^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&MI_[0-9A-F]+)?\\(.+)$') {
                    $vid = $Matches[1].ToUpperInvariant()
                    $productId = $Matches[2].ToUpperInvariant()
                    $serial = $Matches[3]
                    if ($serial -match '&') { $serial = $id }
                    $key = "android|$vid|$productId"
                    if (-not $seenAdb.ContainsKey($key)) {
                        $seenAdb[$key] = $true
                        $name = [string]$desc
                        if (-not $name -or $name -match '(?i)^(USB Composite Device|USB Device|WinUsb Device|ADB Interface|ADB Device|Android ADB Interface|Android Composite ADB Interface)$') {
                            $name = "Android phone"
                        }
                        $phones.Add(@{
                            id               = $serial
                            name             = $name
                            device_class     = "ADB"
                            connection       = "adb"
                            os_hint          = "android"
                            instance_id      = $id
                            has_drive_letter = $false
                            live_acquire     = $true
                            capability_label = "SUPPORTED_DIRECT_LOGICAL"
                            guidance         = "Phone is connected with USB debugging. Unlock the screen and tap Allow if the phone asks to trust this computer."
                            ios_backup_roots = @()
                        })
                    }
                }
            }
            foreach ($line in @($Lines)) {
                $text = [string]$line
                if ($text -match '(?i)Instance ID:\s+(\S+)') {
                    . $commit
                    $id = [string]$Matches[1]
                    $desc = ""
                    $status = ""
                    $adb = $false
                    continue
                }
                if ($text -match '(?i)Device Description:\s+(.+?)\s*$') { $desc = [string]$Matches[1].Trim(); continue }
                if ($text -match '(?i)Status:\s+(\S+)') { $status = [string]$Matches[1]; continue }
                if ($text -match '(?i)SubClass_42&Prot_01|MS_COMP_ADB') { $adb = $true }
            }
            . $commit
            return [object[]]$phones.ToArray()
        }
        function Test-WpdIdCurrentlyAttached {
            param([string]$WpdId, $StartedIds)
            if (-not $WpdId -or $null -eq $StartedIds) { return $false }
            $norm = ($WpdId -replace '#', '\').ToUpperInvariant()
            $vid = $null; $prodId = $null; $serial = $null
            if ($norm -match 'VID_([0-9A-F]{4})') { $vid = $Matches[1] }
            if ($norm -match 'PID_([0-9A-F]{4})') { $prodId = $Matches[1] }
            if ($norm -match 'VID_[0-9A-F]{4}&PID_[0-9A-F]{4}[^\\]*\\([^\\&]+)') { $serial = $Matches[1] }
            if (-not $vid -or -not $prodId) { return $false }
            foreach ($started in $StartedIds) {
                $s = ([string]$started).ToUpperInvariant()
                if ($s -notmatch ("VID_" + $vid) -or $s -notmatch ("PID_" + $prodId)) { continue }
                if ($serial -and $s -match [regex]::Escape($serial)) { return $true }
                if (-not $serial) { return $true }
            }
            return $false
        }
        function Get-AndroidUsbStates {
            # V1.31.7: what Windows shows of Android phones on USB, from pnputil-style lines (pnputil or registry).
            #   MtpKeys - VID|PID of phones with a running File transfer (MTP) interface, whatever Windows calls them
            #             (many phones are listed only by a model code such as SM-A546E or CPH2451).
            #   Ready   - phones with a running File transfer interface that Windows' portable-device list does not
            #             show (the collection finds them by their USB vendor and product ID).
            #   Rows    - phones that are plugged in but not usable yet (charging only, or no Windows driver for their
            #             interface), with what to do on the phone. Before, such a phone was simply not listed.
            param([string[]]$Lines)
            $phoneVendors = @('18D1', '2717', '2A70', '22D9', '2D95', '22B8', '339B', '2A45', '2E04', '29A9', '2B4C')
            $maybeVendors = @('04E8', '0FCE', '1004', '0B05', '0E8D', '05C6', '12D1', '19D2', '0BB4')
            $notPhoneWords = '(?i)modem|network|broadband|mobile connect|card reader|keyboard|mouse|headset|webcam|printer|bluetooth|mass storage|ssd|flash drive'
            $phoneWords = '(?i)android|phone|galaxy|pixel|redmi|xiaomi|poco|oneplus|oppo|vivo|realme|iqoo|moto|nokia|honor|huawei|nothing|infinix|tecno|itel|xperia|mobile'
            $genericDesc = '(?i)^(USB Composite Device|USB Device|WinUsb Device|ADB Interface|ADB Device|Android ADB Interface|Android Composite ADB Interface|MTP USB Device|USB Input Device|Generic USB Hub|USB Mass Storage Device)$'
            # Names used only when nothing better is shown: the File transfer interface carries the phone's own name.
            $fallbackDesc = '(?i)Composite Device$|ADB Interface$|Modem$'
            $groups = @{}
            $block = $null
            $flush = {
                if ($block -and $block.Id -match '(?i)^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(&MI_[0-9A-F]{2})?\\(.+)$') {
                    $vid = $Matches[1].ToUpperInvariant()
                    $prodId = $Matches[2].ToUpperInvariant()
                    $iface = [bool]$Matches[3]
                    $tail = [string]$Matches[4]
                    if ($vid -ne '05AC') {
                        $key = "android|$vid|$prodId"
                        if (-not $groups.ContainsKey($key)) {
                            $groups[$key] = @{ Vid = $vid; ProdId = $prodId; Serial = ''; Instance = ''; Desc = ''; Phone = $false; AdbOk = $false; AdbBad = $false; MtpOk = $false; MtpBad = $false; MsMtp = $false; MtpDesc = ''; FallbackDesc = '' }
                        }
                        $g = $groups[$key]
                        $ids = [string]$block.Ids
                        $started = ((-not $block.Status) -or $block.Status -eq 'Started')
                        $isAdb = ($ids -match '(?i)SubClass_42&Prot_01|MS_COMP_ADB')
                        $isMtp = (($ids -match '(?i)MS_COMP_MTP') -or ($ids -match '(?i)Class_06&SubClass_01&Prot_01'))
                        if ($isAdb) { if ($started) { $g.AdbOk = $true } else { $g.AdbBad = $true } }
                        if ($isMtp) { if ($started) { $g.MtpOk = $true } else { $g.MtpBad = $true } }
                        if ($ids -match '(?i)MS_COMP_MTP') { $g.MsMtp = $true }
                        if ((-not $iface) -and $tail -notmatch '&') { $g.Serial = $tail; $g.Instance = $block.Id }
                        if (-not $g.Instance) { $g.Instance = $block.Id }
                        $words = ([string]$block.Desc + ' ' + [string]$block.Mfr)
                        if ($isAdb -or ($phoneVendors -contains $vid) -or ((($maybeVendors -contains $vid) -or $isMtp) -and $words -match $phoneWords -and $words -notmatch $notPhoneWords)) { $g.Phone = $true }
                        if ($block.Desc -and $block.Desc -notmatch $genericDesc) {
                            if ($isMtp -and -not $g.MtpDesc) { $g.MtpDesc = [string]$block.Desc }
                            if ($block.Desc -match $fallbackDesc) { if (-not $g.FallbackDesc) { $g.FallbackDesc = [string]$block.Desc } }
                            elseif (-not $g.Desc) { $g.Desc = [string]$block.Desc }
                        }
                    }
                }
            }
            foreach ($line in @($Lines)) {
                $text = [string]$line
                if ($text -match '(?i)^\s*Instance ID:\s+(\S+)') {
                    $newId = [string]$Matches[1]   # read before the flush, which runs its own matches
                    . $flush
                    $block = @{ Id = $newId; Desc = ''; Mfr = ''; Status = ''; Ids = '' }
                    continue
                }
                if (-not $block) { continue }
                if ($text -match '(?i)^\s*Device Description:\s+(.+?)\s*$') { $block.Desc = [string]$Matches[1]; continue }
                if ($text -match '(?i)^\s*Manufacturer Name:\s+(.+?)\s*$') { $block.Mfr = [string]$Matches[1]; continue }
                if ($text -match '(?i)^\s*Status:\s+(\S+)') { $block.Status = [string]$Matches[1]; continue }
                $block.Ids = [string]$block.Ids + ' ' + $text.Trim()
            }
            . $flush
            $mtpKeys = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
            $rows = @()
            $ready = @()
            foreach ($key in @($groups.Keys)) {
                $g = $groups[$key]
                # A camera uses the same picture-transfer class; Android's File transfer also says MS_COMP_MTP.
                if ($g.MtpOk -and ($g.Phone -or $g.MsMtp)) {
                    [void]$mtpKeys.Add($g.Vid + '|' + $g.ProdId)
                    $ready += @{
                        id               = $g.Instance
                        name             = $(if ($g.MtpDesc) { $g.MtpDesc } elseif ($g.Desc) { $g.Desc } elseif ($g.FallbackDesc) { $g.FallbackDesc } else { 'Android phone (File transfer)' })
                        device_class     = 'WPD'
                        connection       = 'mtp'
                        os_hint          = 'android'
                        instance_id      = $g.Instance
                        has_drive_letter = $false
                        live_acquire     = $true
                        capability_label = 'SUPPORTED_DIRECT_LOGICAL'
                        guidance         = 'Connected phone detected (File transfer). Use Start extraction.'
                        ios_backup_roots = @()
                    }
                }
                if ((-not $g.Phone) -or $g.AdbOk -or $g.MtpOk) { continue }
                $state = 'charging_only'
                $guidance = 'Connected for charging only. Unlock the phone, open the USB notification on it and choose File transfer (for a deeper collection also turn on USB debugging and tap Allow), then Rescan.'
                if ($g.AdbBad -or $g.MtpBad) {
                    $state = 'driver_missing'
                    $guidance = 'Windows has no working driver for this phone''s USB interface (Device Manager shows it with a warning). On the phone choose USB: File transfer, which needs no extra driver, or install the phone maker''s USB driver, then Rescan.'
                }
                $rows += @{
                    id               = $(if ($g.Serial) { $g.Serial } else { $g.Instance })
                    name             = $(if ($g.Desc) { $g.Desc } elseif ($g.FallbackDesc) { $g.FallbackDesc } else { 'Android phone' })
                    device_class     = 'USB'
                    connection       = 'usb'
                    os_hint          = 'android'
                    instance_id      = $g.Instance
                    has_drive_letter = $false
                    live_acquire     = $false
                    state            = $state
                    capability_label = 'NEEDS_ACTION'
                    guidance         = $guidance
                    ios_backup_roots = @()
                }
            }
            return @{ MtpKeys = $mtpKeys; Ready = $ready; Rows = $rows }
        }

        function Read-WpdPhones {
            param([switch]$RegistryOnly)
            $started = Get-StartedUsbInstanceIds -RegistryOnly:$RegistryOnly
            $usbStates = Get-AndroidUsbStates -Lines $script:LastConnectedUsbLines
            $list = @()
            $seen = @{}
            foreach ($regRoot in @(
                "HKLM:\SOFTWARE\Microsoft\Windows Portable Devices\Devices",
                "HKCU:\SOFTWARE\Microsoft\Windows Portable Devices\Devices"
            )) {
                if (-not (Test-Path -LiteralPath $regRoot)) { continue }
                Get-ChildItem -LiteralPath $regRoot -ErrorAction SilentlyContinue | ForEach-Object {
                    $props = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
                    $name = [string]$props.FriendlyName
                    $id = [string]$_.PSChildName
                    if (-not $name) { $name = $id }
                    if (-not $id) { return }
                    if ($name -match '(?i)^(Local Disk|Windows|Network|CD Drive|DVD|Removable Disk|New Volume|CCCOMA_|RUFUS_)') { return }
                    $mtpPhone = $false
                    if ($id -match '(?i)VID_([0-9A-F]{4}).*?PID_([0-9A-F]{4})') { $mtpPhone = $usbStates.MtpKeys.Contains($Matches[1] + '|' + $Matches[2]) }
                    $looksPhone = ($name -match '(?i)iPhone|iPad|iPod|Apple|Android|Samsung|Galaxy|Xiaomi|Redmi|Huawei|Honor|OnePlus|Pixel|Oppo|Vivo|Tecno|Infinix|Motorola|Nokia|Realme|Nothing|Phone') -or
                        ($id -match '(?i)VID_(05AC|18D1|04E8|2717|2A45|0E8D|2A70|19D2|22D9|29A9|05C6|12D1|22B8|0BB4|0FCE|1004|0B05|2B4C|2D95|339B|2E04)') -or $mtpPhone
                    if (-not $looksPhone) { return }
                    if (-not (Test-WpdIdCurrentlyAttached -WpdId $id -StartedIds $started)) { return }
                    $os = "android"
                    if ($name -match '(?i)iPhone|iPad|iPod|Apple' -or $id -match '(?i)VID_05AC') { $os = "ios" }
                    $key = ($name + "|" + $os).ToLowerInvariant()
                    if ($id -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
                        $key = $os + "|" + $Matches[1].ToUpperInvariant() + "|" + $Matches[2].ToUpperInvariant()
                    }
                    if ($seen.ContainsKey($key)) { return }
                    $seen[$key] = $true
                    $list += @{
                        id               = $id
                        name             = $name
                        device_class     = "WPD"
                        connection       = "mtp"
                        os_hint          = $os
                        instance_id      = $id
                        has_drive_letter = $false
                        live_acquire     = $true
                        capability_label = "SUPPORTED_DIRECT_LOGICAL"
                        guidance         = "Connected phone detected. Use Start extraction."
                        ios_backup_roots = @()
                    }
                }
            }
            foreach ($adbPhone in @(Get-ConnectedAdbPhones -Lines $script:LastConnectedUsbLines)) {
                $key = ""
                if ([string]$adbPhone.instance_id -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
                    $key = "android|" + $Matches[1].ToUpperInvariant() + "|" + $Matches[2].ToUpperInvariant()
                }
                if (-not $key) { continue }
                if ($seen.ContainsKey($key)) {
                    $list = @($list | Where-Object {
                        $rowKey = ""
                        if ([string]$_.instance_id -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
                            $rowKey = "android|" + $Matches[1].ToUpperInvariant() + "|" + $Matches[2].ToUpperInvariant()
                        }
                        $rowKey -ne $key
                    })
                }
                $seen[$key] = $true
                $list += $adbPhone
            }
            foreach ($row in @(@($usbStates.Ready) + @($usbStates.Rows))) {
                $rowKey = ""
                if ([string]$row.instance_id -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
                    $rowKey = "android|" + $Matches[1].ToUpperInvariant() + "|" + $Matches[2].ToUpperInvariant()
                }
                if (-not $rowKey -or $seen.ContainsKey($rowKey)) { continue }
                $seen[$rowKey] = $true
                $list += $row
            }
            return $list
        }
        function Get-UsbTransferProcesses {
            # V1.31.6: phone copies in progress on this PC. A copy process whose parent is gone is a
            # leftover of a stopped or crashed collection, not a copy in progress: it no longer counts.
            # (Before, any leftover froze the phone list, so an unplugged iPhone stayed listed and a
            # newly plugged Android never appeared.)
            $out = @()
            try {
                $all = @(Get-CimInstance Win32_Process -OperationTimeoutSec 20 -ErrorAction SilentlyContinue)
                $alive = @{}
                foreach ($p in $all) { $alive[[int]$p.ProcessId] = $true }
                foreach ($p in $all) {
                    $cmd = [string]$p.CommandLine
                    if (-not $cmd) { continue }
                    if ($cmd -match '--verify\b' -or $cmd -match 'recover-stalled-ios-backup') { continue }
                    $what = ""
                    if ($cmd -match 'ios_usbmux_backup') { $what = "iPhone backup" }
                    elseif ($cmd -match 'ios_usbmux_afc') { $what = "iPhone shared files" }
                    elseif ($cmd -match 'native_ios_job') { $what = "iPhone collection" }
                    elseif ($cmd -match 'native_mtp_job') { $what = "Android collection" }
                    elseif ($cmd -match 'host-mobile-acquire|acq_run_') { $what = "phone collection" }
                    elseif (([string]$p.Name) -match '^(adb|adb\.exe)$' -and $cmd -match '\s(pull|backup|exec-out|shell)\b') { $what = "Android copy" }
                    if (-not $what) { continue }
                    if (-not $alive.ContainsKey([int]$p.ParentProcessId)) { continue }
                    $out += [pscustomobject]@{ Pid = [int]$p.ProcessId; What = $what }
                }
            } catch { }
            return $out
        }
        function Set-UsbStayAwake {
            param([bool]$On)
            if (-not ('AetherisStayAwake' -as [type])) {
                Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class AetherisStayAwake { [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags); }'
            }
            $flags = $(if ($On) { [uint32]0x80000003 } else { [uint32]0x80000000 })
            try { [AetherisStayAwake]::SetThreadExecutionState($flags) | Out-Null } catch {}
        }
        function Write-DeviceCacheFiles {
            param($List, [string]$Source, [string]$Note)
            $list = @($List)
            $json = if ($list.Count -eq 0) { "[]" } elseif ($list.Count -eq 1) { "[" + ($list[0] | ConvertTo-Json -Compress -Depth 6) + "]" } else { ($list | ConvertTo-Json -Compress -Depth 6) }
            [System.IO.File]::WriteAllText($CacheFile, $json)
            $acq = @()
            foreach ($d in $list) {
                $os = [string]$d.os_hint
                $conn = [string]$d.connection
                $adapter = "android_mtp"
                $status = "wpd"
                if ($os -eq "ios") { $adapter = "ios_lockdown" }
                elseif ($conn -eq "adb") { $adapter = "android_adb"; $status = "adb" }
                elseif ($conn -eq "usb") { $adapter = "android_usb"; $status = [string]$d.state }
                $acq += @{
                    adapter     = $adapter
                    device_id   = [string]$d.id
                    label       = [string]$d.name
                    status      = $status
                    os_family   = $(if ($os) { $os } else { "android" })
                    instance_id = [string]$d.instance_id
                    connection  = $conn
                    guidance    = [string]$d.guidance
                    state       = [string]$d.state
                }
            }
            $acqBody = @{
                ok          = $true
                devices     = $acq
                warnings    = @()
                count       = $acq.Count
                scanned_utc = (Get-Date).ToUniversalTime().ToString("o")
                source      = $Source
                note        = $Note
            }
            [System.IO.File]::WriteAllText($AcqFile, ($acqBody | ConvertTo-Json -Compress -Depth 6))
        }
        function Update-DeviceCacheOnce {
            # One refresh of the phone lists the page reads. pnputil is not run during a phone copy
            # (it can reset USB); the registry lists the devices Windows is running without touching
            # USB, so the list follows the cables during a copy too, and when pnputil is busy.
            $transfer = @(Get-UsbTransferProcesses)
            $source = "pnputil"
            $note = ""
            if ($transfer.Count -gt 0) {
                $list = @(Read-WpdPhones -RegistryOnly)
                $source = "registry"
                $note = "A phone copy is running on this computer (" + ((@($transfer | ForEach-Object { $_.What }) | Select-Object -Unique) -join ", ") + "); phones are listed from Windows without re-scanning USB."
            } else {
                $list = @(Read-WpdPhones)
                if (-not $script:EnumFresh) {
                    $list = @(Read-WpdPhones -RegistryOnly)
                    $source = "registry"
                    $note = "Windows USB enumeration (pnputil) is busy; phones are listed from the registry meanwhile."
                }
            }
            # V1.31.9: write first. A failing stay-awake call threw before the write on every pass, so the
            # list stayed at whatever the helper saw when it started.
            Write-DeviceCacheFiles -List $list -Source $source -Note $note
            try { Set-UsbStayAwake (($list.Count -gt 0) -or ($transfer.Count -gt 0)) } catch {}
            return $source
        }
        if ($env:AETHERIS_DEVICE_REFRESH_FUNCTIONS_ONLY -eq "1") { return }
        $statusFile = Join-Path $env:TEMP "aetheris_device_refresh_status.json"
        while ($true) {
            $source = "pnputil"
            $status = @{ ok = $true; at_utc = (Get-Date).ToUniversalTime().ToString("o"); error = "" }
            try { $source = Update-DeviceCacheOnce } catch { $status.ok = $false; $status.error = [string]$_.Exception.Message }
            $status.source = $source
            try { [System.IO.File]::WriteAllText($statusFile, ($status | ConvertTo-Json -Compress)) } catch {}
            Start-Sleep -Seconds $(if ($source -eq "registry") { 10 } else { 15 })
        }
    } -ArgumentList $script:DeviceCacheFile, $script:AcqDevicesCacheFile
    $script:DeviceRefreshStartedAt = Get-Date
}

# V1.31.9: restart the background refresh when it has stopped or stopped writing (checked every 30 s from
# the request loop). The phone lists themselves are read live; this keeps the background file current for
# the PowerShell side.
function Test-MobileDeviceRefresh {
    $now = Get-Date
    if ($script:DeviceRefreshCheckedAt -and (($now - $script:DeviceRefreshCheckedAt).TotalSeconds -lt 30)) { return }
    $script:DeviceRefreshCheckedAt = $now
    try {
        $job = $script:DeviceRefreshJob
        $stale = $false
        if ($job -and $job.State -eq "Running") {
            $since = $script:DeviceRefreshStartedAt
            $written = $null
            if (Test-Path -LiteralPath $script:AcqDevicesCacheFile) { $written = (Get-Item -LiteralPath $script:AcqDevicesCacheFile).LastWriteTime }
            if ($since -and (($now - $since).TotalSeconds -gt 120) -and ((-not $written) -or (($now - $written).TotalSeconds -gt 120))) { $stale = $true }
        }
        if ($job -and $job.State -eq "Running" -and -not $stale) { return }
        if ($job) {
            try { Stop-Job $job -ErrorAction SilentlyContinue } catch {}
            try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch {}
            $script:DeviceRefreshJob = $null
        }
        Start-MobileDeviceRefresh
    } catch {}
}

function Write-HostAdapterCache {
    $body = @{
        ok = $true
        adapters = @(
            @{ name = "android_adb"; os_family = "android"; available = $true; reason = "Host USB helper (USB debugging when authorised)"; via = "host" }
            @{ name = "android_mtp"; os_family = "android"; available = $true; reason = "Windows MTP/WPD ready (file transfer, no USB debugging)"; via = "host" }
            @{ name = "ios_lockdown"; os_family = "ios"; available = $true; reason = "Host USB helper (unlock and Trust This Computer)"; via = "host" }
            @{ name = "sim_reader"; os_family = "sim"; available = $true; reason = "PC/SC on the examiner host"; via = "host" }
            @{ name = "removable_media"; os_family = "storage"; available = $true; reason = "Host removable volumes via /drives"; via = "host" }
        )
    }
    [System.IO.File]::WriteAllText($script:AdaptersCacheFile, ($body | ConvertTo-Json -Compress -Depth 6))
}

function Get-ConnectedMobileDevices {
    return @(Read-MobileDeviceCacheFile)
}

function Get-ConnectedMobileDevices-LegacyUnused {
    $devices = New-Object System.Collections.Generic.List[object]
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $backupRoots = @(Get-IosBackupRoots)
    try {

    function Add-MobileDevice {
        param(
            [string]$Id,
            [string]$Name,
            [string]$DeviceClass = "",
            [string]$Connection = "usb",
            [string]$OsHint = "other",
            [string]$InstanceId = ""
        )
        if (-not $Id -or -not $Name) { return }
        if (-not $seen.Add($Id)) { return }
        # Prefer the friendly WPD name over composite USB entries with the same serial.
        $devices.Add(@{
            id               = $Id
            name             = $Name
            device_class     = $DeviceClass
            connection       = $Connection
            os_hint          = $OsHint
            instance_id      = $InstanceId
            has_drive_letter = $false
            live_acquire     = $true
            capability_label = "SUPPORTED_DIRECT_LOGICAL"
            guidance         = "Connected phone detected. Use Start extraction to create a logical image package under forensic-mobile-acquisitions\{job_id}."
            ios_backup_roots = [string[]]($(if ($OsHint -eq "ios") { @($backupRoots) } else { @() }))
        })
    }

    # Registry only. Shell.Application and Get-PnpDevice can hang for minutes
    # on a wedged MTP stack and would block Identify + Start extraction.
    $phoneVid = '(?i)VID_(05AC|18D1|04E8|2717|2A45|0E8D|2A70|19D2|22D9|29A9|05C6|12D1|22B8|0BB4|0FCE|1004|0B05|2B4C|2A47|04C5|0451|2D95)'
    foreach ($regRoot in @(
        "HKLM:\SOFTWARE\Microsoft\Windows Portable Devices\Devices",
        "HKCU:\SOFTWARE\Microsoft\Windows Portable Devices\Devices"
    )) {
        if (-not (Test-Path -LiteralPath $regRoot)) { continue }
        try {
            Get-ChildItem -LiteralPath $regRoot -ErrorAction SilentlyContinue | ForEach-Object {
                $props = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
                $name = [string]$props.FriendlyName
                $id = [string]$_.PSChildName
                if (-not $name) { $name = $id }
                if ($name -match '(?i)^(Local Disk|Windows|Network|CD Drive|DVD|Removable Disk)') { return }
                $osHint = Resolve-MobileOsHint -FriendlyName $name -InstanceId $id
                if ($osHint -eq "other" -and $name -notmatch '(?i)iPhone|iPad|Android|Phone|Samsung|Xiaomi|Huawei|Honor|OnePlus|Pixel|Oppo|Vivo|Tecno|Infinix|Portable|Galaxy|Redmi|MTP') {
                    if ($id -notmatch $phoneVid) { return }
                    $osHint = "android"
                }
                Add-MobileDevice -Id $id -Name $name -DeviceClass "WPD" -Connection "mtp" -OsHint $osHint -InstanceId $id
            }
        } catch {}
    }
    $usbRoot = "HKLM:\SYSTEM\CurrentControlSet\Enum\USB"
    if (Test-Path -LiteralPath $usbRoot) {
        try {
            Get-ChildItem -LiteralPath $usbRoot -ErrorAction SilentlyContinue |
                Where-Object { $_.PSChildName -match $phoneVid } |
                ForEach-Object {
                    Get-ChildItem -LiteralPath $_.PSPath -ErrorAction SilentlyContinue | ForEach-Object {
                        if (-not (Test-Path -LiteralPath (Join-Path $_.PSPath "Control"))) { return }
                        $props = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
                        $name = [string]$(if ($props.FriendlyName) { $props.FriendlyName } else { $props.DeviceDesc })
                        $id = [string]$props.DeviceID
                        if (-not $id) { $id = [string]$_.PSChildName }
                        if (-not $name) { return }
                        $osHint = Resolve-MobileOsHint -FriendlyName $name -InstanceId $id
                        if ($osHint -eq "other") {
                            if ($id -match '(?i)VID_05AC' -or $name -match '(?i)iPhone|iPad|Apple') { $osHint = "ios" }
                            else { $osHint = "android" }
                        }
                        Add-MobileDevice -Id $id -Name $name -DeviceClass "USB" -Connection "usb" -OsHint $osHint -InstanceId $id
                    }
                }
        } catch {}
    }

    function Get-MobileDeviceRank {
        param($Device)
        $name = [string]$Device.name
        $cls = [string]$Device.device_class
        $score = 0
        if ($cls -eq "WPD") { $score += 100 }
        elseif ($cls -eq "portable") { $score += 80 }
        elseif ($cls -eq "USBDevice") { $score += 10 }
        if ($name -match '(?i)^(Apple )?iPhone|iPad|iPod') { $score += 50 }
        elseif ($name -match '(?i)Android|Samsung|Xiaomi|Huawei|Honor|OnePlus|Pixel|Phone|Galaxy|Redmi|Oppo|Vivo|Tecno') { $score += 40 }
        if ($name -match '(?i)^Apple Mobile Device') { $score -= 80 }
        if ($name -match '(?i)Composite Device|Camera DFU') { $score -= 100 }
        return $score
    }

    # Collapse WPD + MTP + Apple driver interfaces for the same physical phone.
    # One iPhone often appears as both "Apple iPhone" (WPD/MTP) and
    # "Apple Mobile Device USB Device" (Apple Mobile Device Support driver).
    $byKey = @{}
    foreach ($d in $devices) {
        $os = [string]$d.os_hint
        if ($os -eq "ios") {
            # One canonical iOS handset entry unless we later detect multiple UDIDs.
            $key = "ios|handset"
        }
        elseif ($os -eq "android") {
            $vidpid = ""
            foreach ($candidate in @([string]$d.instance_id, [string]$d.id)) {
                if ($candidate -match 'VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})') {
                    $vidpid = ($Matches[1] + "|" + $Matches[2]).ToUpperInvariant()
                    break
                }
            }
            $key = if ($vidpid) { "android|$vidpid" } else { ("android|{0}" -f $d.name).ToLowerInvariant() }
        }
        else {
            $key = ("{0}|{1}" -f $d.name, $os).ToLowerInvariant()
        }
        if (-not $byKey.ContainsKey($key)) {
            $byKey[$key] = $d
            continue
        }
        $existing = $byKey[$key]
        if ((Get-MobileDeviceRank $d) -gt (Get-MobileDeviceRank $existing)) {
            $byKey[$key] = $d
        }
    }

    # Prefer the Apple usbmux UDID (pymobiledevice3) over a Windows WPD instance id.
    # Skip on Android-only scans so a broken host Python cannot stall detection.
    $haveIos = @($byKey.Values | Where-Object { [string]$_.os_hint -eq "ios" }).Count -gt 0
    try {
        if (-not $haveIos) { throw "skip-usbmux-android-only" }
        $mux = @(Get-IosUsbmuxDevices)
        $udids = @($mux | ForEach-Object { [string]$_.udid } | Where-Object { $_ } | Select-Object -Unique)
        if ($udids.Count -eq 0) {
            $ideviceId = Find-IdeviceIdExe
            if ($ideviceId) {
                $listOut = & $ideviceId -l 2>&1 | Out-String
                $udids = @([regex]::Matches($listOut, "[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40}") | ForEach-Object { $_.Value } | Select-Object -Unique)
            }
        }
        if ($udids.Count -ge 1) {
            $wpdIos = $null
            if ($byKey.ContainsKey("ios|handset")) {
                $wpdIos = $byKey["ios|handset"]
                $byKey.Remove("ios|handset")
            }
            foreach ($uid in $udids) {
                $muxDev = @($mux | Where-Object { $_.udid -eq $uid } | Select-Object -First 1)
                $label = if ($muxDev -and $muxDev.name) { [string]$muxDev.name } elseif ($wpdIos -and $udids.Count -eq 1) { [string]$wpdIos.name } else { "Apple iPhone" }
                if ($muxDev -and $muxDev.product) { $label = "$label ($($muxDev.product))" }
                $byKey["ios|$uid"] = [pscustomobject]@{
                    id               = $uid
                    instance_id      = $uid
                    name             = $label
                    os_hint          = "ios"
                    connection       = "usb"
                    device_class     = "usbmux"
                    has_drive_letter = $false
                    live_acquire     = $true
                    capability_label = "SUPPORTED_DIRECT_LOGICAL"
                    ios_backup_roots = $(if ($wpdIos) { $wpdIos.ios_backup_roots } else { $null })
                    guidance         = "Connected iOS device over USB (Apple usbmux). Unlock, Trust This Computer, then Start extraction."
                }
            }
        }
    }
    catch { }

    $script:DeviceCache = @($byKey.Values | Sort-Object { $_.name })
    $script:DeviceCacheAt = Get-Date
    return $script:DeviceCache
    }
    finally {
        $script:DeviceScanBusy = $false
    }
}

function Get-WorkstationDriveVolumes {
    if ($script:DriveCache -and ((Get-Date) - $script:DriveCacheAt).TotalSeconds -lt 8) {
        return $script:DriveCache
    }
    $byLetter = @{}

    function Add-DetectedVolume {
        param($Letter, $Label = '', $DriveType = 'fixed', $Size = 0, $Free = 0, $FileSystem = '')
        $L = ("$Letter").Trim().TrimEnd(':').ToUpper()
        if ($L -notmatch '^[A-Z]$') { return }
        if (-not $byLetter.ContainsKey($L)) {
            $byLetter[$L] = @{
                letter     = $L
                label      = ''
                drive_type = 'fixed'
                size_bytes = [int64]0
                free_bytes = [int64]0
                filesystem = ''
            }
        }
        $row = $byLetter[$L]
        if ($Label) { $row.label = [string]$Label }
        if ($DriveType) { $row.drive_type = [string]$DriveType }
        if ($Size) { $row.size_bytes = [int64]$Size }
        if ($Free) { $row.free_bytes = [int64]$Free }
        if ($FileSystem) { $row.filesystem = [string]$FileSystem }
    }

    # Get-Volume sees USB/JMicron external disks that Win32_LogicalDisk omits.
    try {
        Get-Volume -ErrorAction SilentlyContinue |
            Where-Object { $_.DriveLetter -and "$($_.DriveLetter)" -match '^[A-Z]$' } |
            ForEach-Object {
                $dt = ([string]$_.DriveType).ToLower()
                if ($dt -eq 'cd-rom') { $dt = 'cdrom' }
                Add-DetectedVolume -Letter $_.DriveLetter -Label $_.FileSystemLabel -DriveType $dt `
                    -Size $_.Size -Free $_.SizeRemaining -FileSystem $_.FileSystem
            }
    }
    catch { }
    try {
        Get-Partition -ErrorAction SilentlyContinue |
            Where-Object { $_.DriveLetter -and "$($_.DriveLetter)" -match '^[A-Z]$' } |
            ForEach-Object { Add-DetectedVolume -Letter $_.DriveLetter }
    }
    catch { }
    try {
        Get-CimInstance -ClassName Win32_LogicalDisk -ErrorAction SilentlyContinue |
            Where-Object { $_.DeviceID -match '^[A-Z]:$' } |
            ForEach-Object {
                $driveType = switch ([int]$_.DriveType) {
                    2 { 'removable' }
                    3 { 'fixed' }
                    4 { 'network' }
                    5 { 'cdrom' }
                    default { 'other' }
                }
                Add-DetectedVolume -Letter $_.DeviceID.TrimEnd(':') -Label $_.VolumeName `
                    -DriveType $driveType -Size $_.Size -Free $_.FreeSpace -FileSystem $_.FileSystem
            }
    }
    catch { }
    foreach ($code in 65..90) {
        $name = [string][char]$code
        if ($byLetter.ContainsKey($name)) { continue }
        if (-not (Test-Path -LiteralPath "${name}:\")) { continue }
        $info = $null
        try { $info = [System.IO.DriveInfo]::new("${name}:\") } catch { }
        Add-DetectedVolume -Letter $name `
            -Label $(if ($info -and $info.IsReady) { [string]$info.VolumeLabel } else { '' }) `
            -DriveType $(if ($info) { [string]$info.DriveType.ToString().ToLower() } else { 'other' }) `
            -Size $(if ($info -and $info.IsReady) { [int64]$info.TotalSize } else { 0 }) `
            -Free $(if ($info -and $info.IsReady) { [int64]$info.TotalFreeSpace } else { 0 })
    }

    # Source availability is a property of the filesystem, not of the device
    # bus. Many USB/NVMe enclosures report Fixed and many flash readers reserve
    # letters while no readable medium is inserted. Surface ready separately.
    foreach ($row in $byLetter.Values) {
        $isReady = $false
        try {
            $driveInfo = [System.IO.DriveInfo]::new("$($row.letter):\")
            if ($driveInfo.DriveType -ne [System.IO.DriveType]::CDRom -and
                $driveInfo.DriveType -ne [System.IO.DriveType]::Network) {
                $isReady = [bool]$driveInfo.IsReady
            }
        } catch { $isReady = $false }
        $row.ready = $isReady
    }
    $script:DriveCache = @($byLetter.Values | Sort-Object { $_.letter })
    $script:DriveCacheAt = Get-Date
    return $script:DriveCache
}

function Get-RefreshDriveMountsStatus {
    $state = @{
        status  = "idle"
        ok      = $null
        message = "No refresh in progress"
        drives  = @()
        error   = $null
        busy    = [bool]$script:Busy
    }
    if ($script:RefreshStatusFile -and (Test-Path -LiteralPath $script:RefreshStatusFile)) {
        try {
            $raw = Get-Content -Path $script:RefreshStatusFile -Raw -ErrorAction Stop
            $parsed = $raw | ConvertFrom-Json
            $state = @{
                status       = [string]$parsed.status
                ok           = $parsed.ok
                message      = [string]$parsed.message
                drives       = @($parsed.drives)
                required_path = $(if ($parsed.required_path) { [string]$parsed.required_path } else { $null })
                error        = $(if ($parsed.error) { [string]$parsed.error } else { $null })
                log          = $(if ($parsed.log) { [string]$parsed.log } else { "" })
                unchanged     = [bool]$parsed.unchanged
                mount_mode    = $(if ($parsed.mount_mode) { [string]$parsed.mount_mode } else { $null })
                mount_source  = $(if ($parsed.mount_source) { [string]$parsed.mount_source } else { $null })
                wsl_distro    = $(if ($parsed.wsl_distro) { [string]$parsed.wsl_distro } else { $null })
                skipped_drives = @($parsed.skipped_drives)
                compose_base  = $(if ($parsed.compose_base) { [string]$parsed.compose_base } else { $null })
                busy          = [bool]$script:Busy
                started_utc   = $(if ($parsed.started_utc) { [string]$parsed.started_utc } else { $null })
                finished_utc = $(if ($parsed.finished_utc) { [string]$parsed.finished_utc } else { $null })
                job_version   = $(if ($parsed.job_version) { [string]$parsed.job_version } else { $null })
                job_script    = $(if ($parsed.job_script) { [string]$parsed.job_script } else { $refreshJobScript })
                helper_root   = $root
            }
        }
        catch {
            $state.error = "Could not read refresh status: $($_.Exception.Message)"
        }
    }

    # Clear busy when background process finished.
    if ($script:RefreshProcess) {
        try {
            if ($script:RefreshProcess.HasExited) {
                $exitCode = $script:RefreshProcess.ExitCode
                $script:RefreshProcess = $null
                if ($state.status -in @("done", "error", "idle")) {
                    $script:Busy = $false
                }
                elseif ($state.status -eq "running") {
                    $state.status = "error"
                    $state.ok = $false
                    $state.error = "Drive mount refresh exited (code $exitCode) before reporting a result. Try again."
                    $state.message = $state.error
                    $script:Busy = $false
                    @{
                        status = "error"
                        ok = $false
                        message = $state.error
                        error = $state.error
                        drives = @($state.drives)
                    } | ConvertTo-Json -Compress | Set-Content -Path $script:RefreshStatusFile -Encoding UTF8
                }
            }
            else {
                $script:Busy = $true
                if ($state.status -eq "idle") {
                    $state.status = "running"
                    $state.message = "Refreshing drive mounts..."
                }
            }
        }
        catch {
            $script:RefreshProcess = $null
            $script:Busy = $false
        }
    }
    elseif ($state.status -in @("done", "error")) {
        $script:Busy = $false
    }
    $state.busy = [bool]$script:Busy
    return $state
}

function Format-CommandLineArgument {
    # CommandLineToArgvW: 2n backslashes before a closing quote produce n
    # backslashes and end the argument. A path such as G:\ must be emitted as
    # "G:\\" or the quote is eaten and the job sees G:" .
    param([string]$Value)
    $tail = 0
    for ($i = $Value.Length - 1; $i -ge 0; $i--) {
        if ($Value[$i] -ne '\') { break }
        $tail++
    }
    $body = if ($tail -gt 0) { $Value.Substring(0, $Value.Length - $tail) } else { $Value }
    return '"' + $body + ('\' * ($tail * 2)) + '"'
}

function Start-RefreshDriveMountsAsync {
    param([string]$RequiredPath = "")

    if (-not (Test-Path -LiteralPath $refreshJobScript)) {
        throw "Missing script: $refreshJobScript"
    }
    $required = ("$RequiredPath").Trim()
    if ($required) {
        $required = $required -replace '/', '\'
        if ($required -notmatch '^[A-Za-z]:[\\/]') {
            throw "Selected evidence path must be an absolute Windows drive path (for example G:\Evidence\Case01)."
        }
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Windows cannot see the selected evidence path: $required"
        }
    }

    $current = Get-RefreshDriveMountsStatus
    if ($current.status -eq "running" -or ($script:RefreshProcess -and -not $script:RefreshProcess.HasExited)) {
        throw "Refresh already in progress"
    }

    $script:Busy = $true
    Remove-Item -LiteralPath $script:RefreshStatusFile -Force -ErrorAction SilentlyContinue
    @{
        status        = "running"
        ok            = $null
        message       = $(if ($required) { "Checking Docker access to $required..." } else { "Drive mount refresh started..." })
        drives        = @()
        required_path = $(if ($required) { $required } else { $null })
        error         = $null
        log           = ""
        started_utc   = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Compress | Set-Content -Path $script:RefreshStatusFile -Encoding UTF8

    # One command-line string. Start-Process re-quotes array items that contain
    # spaces, and CommandLineToArgvW treats a backslash before the closing quote
    # as an escape: -RequiredPath "G:\" arrives as G:" and the remount aborts
    # before Docker ever sees /host/g. Doubling only the trailing slashes keeps
    # the drive root and still preserves spaces in evidence folder names.
    $cmd = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Format-CommandLineArgument $refreshJobScript),
        "-Root", (Format-CommandLineArgument $root),
        "-StatusFile", (Format-CommandLineArgument $script:RefreshStatusFile)
    ) -join " "
    if ($required) {
        $cmd += " -RequiredPath " + (Format-CommandLineArgument $required)
    }
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $cmd -WindowStyle Hidden -PassThru
    $script:RefreshProcess = $proc

    return @{
        ok            = $true
        started       = $true
        status        = "running"
        required_path = $(if ($required) { $required } else { $null })
        message       = $(if ($required) {
            "Checking and mounting the selected Windows drive. API may restart briefly - keep this page open."
        } else {
            "Drive mount refresh started. API may restart briefly - keep this page open."
        })
        pid           = $proc.Id
    }
}

function Invoke-RefreshDriveMounts {
    param([string]$RequiredPath = "")
    # Back-compat sync path (unused by UI after async change).
    return Start-RefreshDriveMountsAsync -RequiredPath $RequiredPath
}

function Handle-Request {
    param([System.Net.HttpListenerContext]$Context)

    $request = $Context.Request
    $response = $Context.Response
    $path = ($request.Url.AbsolutePath.TrimEnd("/"))
    if (-not $path) { $path = "/" }

    if ($request.HttpMethod -eq "OPTIONS") {
        Write-JsonResponse -Response $response -StatusCode 204 -Body @{}
        return
    }

    if (-not (Test-Authorized -Request $request)) {
        Write-JsonResponse -Response $response -StatusCode 401 -Body @{
            ok    = $false
            error = "Unauthorized"
        }
        return
    }

    if ($request.HttpMethod -eq "GET" -and ($path -eq "/" -or $path -eq "/health")) {
        $runningJobs = @($script:AcquireJobs.Keys | Where-Object {
            $j = $script:AcquireJobs[$_]
            $j.Status -eq "running"
        })
        Write-JsonResponse -Response $response -StatusCode 200 -Body @{
            ok                 = $true
            service            = "host-drive-helper"
            port               = $Port
            bind_host          = $BindHost
            busy               = [bool]$script:Busy
            acquire_jobs       = $runningJobs.Count
            helper_version     = $script:HelperVersion
            helper_pid         = $PID
            helper_script      = $PSCommandPath
            root               = $root
            job_script         = $refreshJobScript
            job_script_version = (Get-JobScriptVersion)
            started_utc        = $script:HelperStartedUtc
            malware_triage     = $true
            malware_remediate  = $true
            computer_name      = $script:HelperComputerName
            client_import      = $true
            import_case_root   = $script:ImportCasesRoot
        }
        return
    }

    if ($request.HttpMethod -eq "GET" -and $path -eq "/acquisition/jobs") {
        $jobs = @()
        $jobsDir = Join-Path $env:TEMP "aetheris_acq_jobs"
        if (Test-Path -LiteralPath $jobsDir) {
            foreach ($f in @(Get-ChildItem -LiteralPath $jobsDir -Filter "*.json" -ErrorAction SilentlyContinue)) {
                try {
                    $meta = Get-Content -LiteralPath $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                    $sid = [int]($(if ($meta.pid) { $meta.pid } else { 0 }))
                    if ($sid -le 0) { continue }
                    if (-not (Get-Process -Id $sid -ErrorAction SilentlyContinue)) { continue }
                    $detail = ""
                    $runName = ""
                    $files = 0
                    $bytes = [int64]0
                    $pf = [string]$meta.progress_file
                    if ($pf -and (Test-Path -LiteralPath $pf)) {
                        $p = Get-Content -LiteralPath $pf -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
                        $detail = [string]$p.detail
                        $runName = [string]$p.run_name
                        $files = [int]$p.files_seen
                        $bytes = [int64]$p.bytes_done
                    }
                    $caseId = [string]$meta.case_id
                    $jobs += @{
                        job_id     = $f.BaseName
                        status     = "running"
                        run_name   = $runName
                        case_id    = $caseId
                        detail     = $detail
                        files_seen = $files
                        bytes_done = $bytes
                    }
                } catch {}
            }
        }
        Write-JsonResponse -Response $response -StatusCode 200 -Body @{ ok = $true; jobs = @($jobs) }
        return
    }

    if ($request.HttpMethod -eq "GET" -and ($path -eq "/acquisition/job" -or $path.StartsWith("/acquisition/job/"))) {
        $jobId = ""
        if ($path.StartsWith("/acquisition/job/") -and $path.Length -gt "/acquisition/job/".Length) {
            $jobId = $path.Substring("/acquisition/job/".Length).Trim("/")
        }
        if (-not $jobId -and $request.Url.Query) {
            $q = [System.Web.HttpUtility]::ParseQueryString($request.Url.Query)
            $jobId = [string]$q["id"]
        }
        # Fallback without System.Web
        if (-not $jobId -and $request.Url.Query -match '[?&]id=([^&]+)') {
            $jobId = [uri]::UnescapeDataString($Matches[1])
        }
        $status = Get-AcquireJobStatus -JobId $jobId
        # Prefer a slim status for large forensic result files. Embedding a multi‑MB
        # result here blocks Docker↔host polling and leaves the UI stuck at ~70%.
        if ($status.status -in @("completed", "failed") -and $jobId -and $script:AcquireJobs.ContainsKey($jobId)) {
            $rf = $script:AcquireJobs[$jobId].ResultFile
            $pf = $script:AcquireJobs[$jobId].ProgressFile
            if ($rf -and (Test-Path -LiteralPath $rf)) {
                try {
                    $len = [int64](Get-Item -LiteralPath $rf).Length
                    $maxInline = 512KB
                    if ($len -le $maxInline) {
                        $rawResult = [System.IO.File]::ReadAllText($rf)
                        $wrapper = "{`"ok`":true,`"status`":`"$($status.status)`",`"job_id`":`"$jobId`",`"result`":$rawResult}"
                        Write-RawJsonResponse -Response $response -StatusCode 200 -Json $wrapper
                        return
                    }
                    # Peek essential fields without loading the whole document into ConvertTo-Json.
                    $head = ""
                    $fs = [System.IO.File]::OpenRead($rf)
                    try {
                        $buf = New-Object byte[] ([Math]::Min(65536, $len))
                        $n = $fs.Read($buf, 0, $buf.Length)
                        $head = [System.Text.Encoding]::UTF8.GetString($buf, 0, $n)
                    } finally { $fs.Close() }
                    $runName = ""
                    if ($head -match '"run_name"\s*:\s*"([^"]+)"') { $runName = $Matches[1] }
                    $stage = $status.status
                    if ($head -match '"stage_reached"\s*:\s*"([^"]+)"') { $stage = $Matches[1] }
                    $okVal = ($status.status -eq "completed")
                    if ($head -match '"ok"\s*:\s*false') { $okVal = $false }
                    Write-JsonResponse -Response $response -StatusCode 200 -Body @{
                        ok           = $true
                        status       = $status.status
                        job_id       = $jobId
                        result_file  = $rf
                        result_bytes = $len
                        progress     = $pf
                        result       = @{
                            ok            = $okVal
                            run_name      = $runName
                            stage_reached = $(if ($stage) { $stage } else { "complete" })
                            paths         = @{}
                            errors        = @()
                            warnings      = @("Full host result is large ($([math]::Round($len/1MB,1)) MB); API will recover sealed paths from disk.")
                            acquisition_record = @{}
                            evidence_package = @{ run_name = $runName; complete = $okVal; extraction_data = @() }
                        }
                    }
                    return
                } catch {}
            }
        }
        Write-JsonResponse -Response $response -StatusCode 200 -Body $status
        return
    }

    if ($request.HttpMethod -eq "GET" -and $path -eq "/drives") {
        try {
            $volumes = Get-WorkstationDriveVolumes
            $letters = @($volumes | ForEach-Object { $_.letter })
            $mobileDevices = @(Get-ConnectedMobileDevices)
            Write-JsonResponse -Response $response -StatusCode 200 -Body @{
                ok             = $true
                drives         = $letters
                volumes        = $volumes
                count          = $letters.Count
                mobile_devices = $mobileDevices
                mobile_count   = $mobileDevices.Count
            }
        }
        catch {
            Write-JsonResponse -Response $response -StatusCode 500 -Body @{
                ok    = $false
                error = $_.Exception.Message
            }
        }
        return
    }

    if ($request.HttpMethod -eq "GET" -and ($path -eq "/mobile-devices" -or $path -eq "/mobile_devices")) {
        try {
            $mobileDevices = @(Get-ConnectedMobileDevices)
            Write-JsonResponse -Response $response -StatusCode 200 -Body @{
                ok             = $true
                mobile_devices = $mobileDevices
                count          = $mobileDevices.Count
                ios_backup_roots = @()
            }
        }
        catch {
            Write-JsonResponse -Response $response -StatusCode 500 -Body @{
                ok    = $false
                error = $_.Exception.Message
            }
        }
        return
    }

    if ($request.HttpMethod -eq "GET" -and $path -eq "/acquisition/adapters") {
        try {
            if (-not (Test-Path -LiteralPath $script:AdaptersCacheFile)) { Write-HostAdapterCache }
            $raw = Get-Content -LiteralPath $script:AdaptersCacheFile -Raw
            Write-RawJsonResponse -Response $response -StatusCode 200 -Json $raw
        } catch {
            Write-JsonResponse -Response $response -StatusCode 200 -Body @{
                ok = $true
                adapters = @(
                    @{ name = "android_mtp"; os_family = "android"; available = $true; reason = "Windows MTP/WPD ready"; via = "host" }
                    @{ name = "android_adb"; os_family = "android"; available = $true; reason = "Host USB helper"; via = "host" }
                    @{ name = "ios_lockdown"; os_family = "ios"; available = $true; reason = "Host USB helper"; via = "host" }
                )
            }
        }
        return
    }

    if ($request.HttpMethod -eq "GET" -and $path -eq "/acquisition/devices") {
        try {
            $devices = @(); $warnings = @()
            $iosSeen = @{}
            $sim = @{ available = $false; readers = @(); reason = "" }
            try { $sim = Get-PcscSimStatus } catch {}
            foreach ($reader in @($sim.readers)) {
                if ($reader) {
                    $devices += @{ adapter = "sim_reader"; device_id = [string]$reader; label = ("SIM reader (" + $reader + ")"); status = "reader"; os_family = "sim" }
                }
            }
            # WPD/MTP phones (old Android without USB debugging, file-transfer mode).
            $adbIds = @($devices | Where-Object { $_.adapter -eq "android_adb" } | ForEach-Object { [string]$_.device_id })
            foreach ($wpd in @(Get-ConnectedMobileDevices)) {
                $os = [string]$wpd.os_hint
                $wid = [string]$wpd.id
                $winst = [string]$wpd.instance_id
                if ($os -eq "ios") {
                    if ($iosSeen.ContainsKey($wid)) { continue }
                    if ($iosSeen.Count -gt 0) { continue }
                    $devices += @{
                        adapter     = "ios_lockdown"
                        device_id   = $wid
                        label       = [string]$wpd.name
                        status      = "wpd"
                        os_family   = "ios"
                        instance_id = $winst
                        connection  = [string]$wpd.connection
                    }
                    continue
                }
                $dupAdb = $false
                foreach ($sid in $adbIds) {
                    if (-not $sid) { continue }
                    if ($wid -match [regex]::Escape($sid) -or $winst -match [regex]::Escape($sid)) {
                        $dupAdb = $true
                        break
                    }
                }
                if ($dupAdb) { continue }
                $already = $false
                foreach ($ex in $devices) {
                    if ([string]$ex.device_id -eq $wid) { $already = $true; break }
                }
                if ($already) { continue }
                $conn = [string]$wpd.connection
                $adapterName = "android_mtp"
                $statusName = $(if ($conn -eq "mtp") { "mtp" } else { "wpd" })
                if ($conn -eq "adb") { $adapterName = "android_adb"; $statusName = "adb" }
                if ($conn -eq "usb") { $adapterName = "android_usb"; $statusName = [string]$wpd.state }
                $devices += @{
                    adapter     = $adapterName
                    device_id   = $wid
                    label       = $(if ($wpd.name) { [string]$wpd.name } else { "Android phone" })
                    status      = $statusName
                    os_family   = "android"
                    instance_id = $winst
                    connection  = $conn
                    guidance    = [string]$wpd.guidance
                }
            }
            Write-JsonResponse -Response $response -StatusCode 200 -Body @{ ok = $true; devices = @($devices); warnings = @($warnings); count = @($devices).Count }
        } catch {
            # Still return WPD/MTP phones so the UI can detect without ADB/usbmux.
            $fallback = @()
            try {
                foreach ($wpd in @(Get-ConnectedMobileDevices)) {
                    $os = [string]$wpd.os_hint
                    $conn = [string]$wpd.connection
                    $adapterName = "android_mtp"
                    if ($os -eq "ios") { $adapterName = "ios_lockdown" }
                    elseif ($conn -eq "adb") { $adapterName = "android_adb" }
                    elseif ($conn -eq "usb") { $adapterName = "android_usb" }
                    $fallback += @{
                        adapter     = $adapterName
                        device_id   = [string]$wpd.id
                        label       = [string]$wpd.name
                        status      = $(if ($conn -eq "adb") { "adb" } else { "wpd" })
                        os_family   = $(if ($os) { $os } else { "android" })
                        instance_id = [string]$wpd.instance_id
                        connection  = $conn
                    }
                }
            } catch {}
            Write-JsonResponse -Response $response -StatusCode 200 -Body @{
                ok = $true
                devices = $fallback
                warnings = @($_.Exception.Message)
                count = $fallback.Count
            }
        }
        return
    }
    if ($request.HttpMethod -eq "POST" -and $path -eq "/acquisition/cancel") {
        try {
            $payload = Read-RequestJson -Request $request
            $jobId = ""
            $progressFile = ""
            if ($payload.job_id) { $jobId = [string]$payload.job_id }
            if ($payload.progress_file) { $progressFile = [string]$payload.progress_file }
            $result = Stop-AcquireJob -JobId $jobId -ProgressFile $progressFile
            Write-JsonResponse -Response $response -StatusCode 200 -Body $result
        }
        catch {
            Write-JsonResponse -Response $response -StatusCode 500 -Body @{
                ok    = $false
                error = $_.Exception.Message
            }
        }
        return
    }
    if ($request.HttpMethod -eq "POST" -and $path -eq "/acquisition/remove") {
        try {
            $payload = Read-RequestJson -Request $request
            $extra = @{}
            if ($payload.paths) {
                foreach ($k in @("original", "working", "logs", "hashes", "exports", "case_root", "output_path")) {
                    $v = [string]$payload.paths.$k
                    if ($v) { $extra[$k] = $v }
                }
            }
            $jobId = ""
            $runName = ""
            $caseId = ""
            $progressFile = ""
            if ($payload.job_id) { $jobId = [string]$payload.job_id }
            if ($payload.run_name) { $runName = [string]$payload.run_name }
            if ($payload.case_id) { $caseId = [string]$payload.case_id }
            if ($payload.progress_file) { $progressFile = [string]$payload.progress_file }
            $result = Remove-AcquireRunData -JobId $jobId -RunName $runName -CaseId $caseId -ProgressFile $progressFile -ExtraPaths $extra
            Write-JsonResponse -Response $response -StatusCode 200 -Body $result
        }
        catch {
            Write-JsonResponse -Response $response -StatusCode 500 -Body @{
                ok    = $false
                error = $_.Exception.Message
            }
        }
        return
    }
    if ($request.HttpMethod -eq "POST" -and $path -eq "/acquisition/preview") {
        try {
            $payload = Read-RequestJson -Request $request
            $adapterName = [string]$payload.adapter
            $deviceId = [string]$payload.device_id
            $nameHint = [string]($(if ($payload.label) { $payload.label } elseif ($payload.device_name) { $payload.device_name } else { "" }))
            if (-not $adapterName -or -not $deviceId) { throw "adapter and device_id are required" }
            # MTP/WPD identify must not depend on a working host Python (broken venvs
            # previously returned 500 and the UI showed a Docker USB error).
            if ($adapterName -in @("android_mtp", "android_adb", "ios_lockdown")) {
                $native = New-HostDevicePreview -Adapter $adapterName -DeviceId $deviceId -NameHint $nameHint
                Write-JsonResponse -Response $response -StatusCode 200 -Body $native
                return
            }
            $backend = Join-Path $root "backend"
            $py = Get-HostPython
            if (-not $py) {
                Write-JsonResponse -Response $response -StatusCode 200 -Body (New-HostDevicePreview -Adapter $adapterName -DeviceId $deviceId)
                return
            }
            $pt = Join-Path $root "tools\platform-tools"
            $lid = Join-Path $root "tools\libimobiledevice"
            $extraPath = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools"
            $env:PATH = ($pt + ";" + $lid + ";" + $extraPath + ";" + $env:PATH)
            $env:PYTHONPATH = $backend
            $stamp = [guid]::NewGuid().ToString("N")
            $tmpPy = Join-Path $env:TEMP ("acq_preview_" + $stamp + ".py")
            $tmpJson = Join-Path $env:TEMP ("acq_preview_" + $stamp + ".json")
            $tmpOut = Join-Path $env:TEMP ("acq_preview_" + $stamp + ".out")
            $tmpErr = Join-Path $env:TEMP ("acq_preview_" + $stamp + ".err")
            $previewPayload = @{ adapter = [string]$payload.adapter; device_id = [string]$payload.device_id }
            [System.IO.File]::WriteAllText($tmpJson, ($previewPayload | ConvertTo-Json -Depth 6), [System.Text.UTF8Encoding]::new($false))
            $pySource = "import json,sys`nfrom pathlib import Path`nfrom app.services.mobile_acquire import CollectionOrchestrator, DeviceDetector`npayload=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))`nprint(json.dumps(CollectionOrchestrator(DeviceDetector()).preview(payload['adapter'], payload['device_id']), default=str))`n"
            [System.IO.File]::WriteAllText($tmpPy, $pySource, [System.Text.UTF8Encoding]::new($false))
            $proc = Start-Process -FilePath $py -ArgumentList @($tmpPy, $tmpJson) -WorkingDirectory $backend -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr -NoNewWindow -Wait -PassThru
            $stdout = if (Test-Path -LiteralPath $tmpOut) { Get-Content -LiteralPath $tmpOut -Raw -EA SilentlyContinue } else { "" }
            $stderr = if (Test-Path -LiteralPath $tmpErr) { Get-Content -LiteralPath $tmpErr -Raw -EA SilentlyContinue } else { "" }
            foreach ($f in @($tmpPy, $tmpJson, $tmpOut, $tmpErr)) { Remove-Item -LiteralPath $f -Force -EA SilentlyContinue }
            $jsonLine = $null
            if ($stdout) { $jsonLine = ($stdout -split "`r?`n" | Where-Object { $_.Trim().StartsWith("{") } | Select-Object -Last 1) }
            if (-not $jsonLine) { throw ("Preview produced no JSON: " + $stdout + " " + $stderr) }
            Write-JsonResponse -Response $response -StatusCode 200 -Body ($jsonLine | ConvertFrom-Json)
        } catch {
            try {
                Write-JsonResponse -Response $response -StatusCode 200 -Body (New-HostDevicePreview -Adapter ([string]$payload.adapter) -DeviceId ([string]$payload.device_id))
            } catch {
                Write-JsonResponse -Response $response -StatusCode 500 -Body @{ ok = $false; error = $_.Exception.Message }
            }
        }
        return
    }

    if ($request.HttpMethod -eq "POST" -and $path -eq "/malware/triage") {
        if (Test-UsbAcquireInProgress) { Write-UsbBusyResponse -Response $response; return }
        try {
            $body = Start-MalwareTriage -Root $root -Payload (Read-RequestJson -Request $request)
            $code = 409
            if ($body.ok) { $code = 202 }
            Write-JsonResponse -Response $response -StatusCode $code -Body $body
        } catch {
            Write-JsonResponse -Response $response -StatusCode 400 -Body @{ ok = $false; error = $_.Exception.Message }
        }
        return
    }
    if ($request.HttpMethod -eq "GET" -and $path -match '^/malware/triage/([0-9a-f]{32})(/bundle)?$') {
        $tid = $Matches[1]
        if ($Matches[2]) { Send-MalwareTriageBundle -Response $response -Root $root -Id $tid; return }
        $st = Get-MalwareTriageStatus -Root $root -Id $tid
        $code = 200
        if ($st.status -eq 'unknown') { $code = 404 }
        Write-JsonResponse -Response $response -StatusCode $code -Body $st
        return
    }
    if ($request.HttpMethod -eq "POST" -and $path -eq "/malware/remediate") {
        # Incident response: REMOVE malware from the connected phone. This CHANGES the phone (consent required).
        if (Test-UsbAcquireInProgress) { Write-UsbBusyResponse -Response $response; return }
        try {
            $body = Start-MalwareRemediation -Root $root -Payload (Read-RequestJson -Request $request)
            $code = if ($body.ok) { 202 } else { 409 }
            Write-JsonResponse -Response $response -StatusCode $code -Body $body
        } catch {
            Write-JsonResponse -Response $response -StatusCode 400 -Body @{ ok = $false; error = $_.Exception.Message }
        }
        return
    }
    if ($request.HttpMethod -eq "GET" -and $path -match '^/malware/remediate/([0-9a-f]{32})$') {
        $st = Get-MalwareRemediationStatus -Root $root -Id $Matches[1]
        $code = if ($st.status -eq 'unknown') { 404 } else { 200 }
        Write-JsonResponse -Response $response -StatusCode $code -Body $st
        return
    }
    if ($request.HttpMethod -eq "POST" -and $path -eq "/acquisition/run") {
        # Async: start Python and return immediately so /health and adapters stay up.
        # Browse / register stay available. Only a live USB acquire blocks another USB acquire.
        if (Test-UsbAcquireInProgress) {
            Write-UsbBusyResponse -Response $response
            return
        }
        try {
            $payload = Read-RequestJson -Request $request
            $caseRoot = [string]$payload.case_root
            if ($caseRoot -match '^[\\/]evidence([\\/]|$)') {
                $rel = ($caseRoot -replace '^[\\/]evidence[\\/]?', '' -replace '/', '\')
                $caseRoot = if ($rel) { Join-Path (Join-Path $root "evidence") $rel } else { Join-Path $root "evidence\cases" }
            } elseif (-not $caseRoot -or $caseRoot -eq "/evidence/cases" -or $caseRoot -eq "\evidence\cases") {
                $caseRoot = Join-Path $root "evidence\cases"
            }
            New-Item -ItemType Directory -Force -Path $caseRoot | Out-Null
            $stamp = [guid]::NewGuid().ToString("N")
            $tmpResult = Join-Path $env:TEMP ("acq_result_" + $stamp + ".json")
            $cloudBoxes = @()
            if ($payload.cloud_mailboxes) { $cloudBoxes = @($payload.cloud_mailboxes) }
            $runPayload = @{
                case_id = [string]$payload.case_id
                evidence_id = [string]$payload.evidence_id
                examiner = [string]($(if ($payload.examiner) { $payload.examiner } else { "host-acquire" }))
                legal_authority = [string]$payload.legal_authority
                case_root = $caseRoot
                adapter = [string]$payload.adapter
                device_id = [string]$payload.device_id
                objective_methods = @($payload.objective_methods)
                authorized_methods = @($payload.authorized_methods)
                method_override = $(if ($payload.method_override) { [string]$payload.method_override } else { $null })
                cable_adapter_asset_id = [string]($(if ($payload.cable_adapter_asset_id) { $payload.cable_adapter_asset_id } else { "" }))
                license_endpoint_id = [string]($(if ($payload.license_endpoint_id) { $payload.license_endpoint_id } else { "" }))
                backup_password = $(if ($null -ne $payload.backup_password -and [string]$payload.backup_password) { [string]$payload.backup_password } else { $null })
                examiner_notes = [string]($(if ($payload.examiner_notes) { $payload.examiner_notes } else { "" }))
                device_label = [string]($(if ($payload.device_label) { $payload.device_label } elseif ($payload.label) { $payload.label } else { "" }))
                device_condition = [string]($(if ($payload.device_condition) { $payload.device_condition } else { "" }))
                network_isolated = $payload.network_isolated
                create_working_copy = [bool]($(if ($null -ne $payload.create_working_copy) { $payload.create_working_copy } else { $true }))
                cloud_mailboxes = $cloudBoxes
                progress_file = [string]($(if ($payload.progress_file) { $payload.progress_file } else { "" }))
                result_file = $tmpResult
            }
            if (-not $runPayload.progress_file) {
                $runPayload.progress_file = Join-Path $caseRoot (".acq_progress_" + [guid]::NewGuid().ToString("N") + ".json")
            }
            $backend = Join-Path $root "backend"
            $adapterName = [string]$runPayload.adapter
            if ($adapterName -eq "android_mtp" -or $adapterName -match 'mtp') {
                Start-NativeMtpAcquireJob -RunPayload $runPayload -Stamp $stamp -TmpResult $tmpResult
                Write-JsonResponse -Response $response -StatusCode 202 -Body @{
                    ok                 = $true
                    async              = $true
                    job_id             = $stamp
                    status             = "running"
                    busy               = $true
                    progress           = [string]$runPayload.progress_file
                    via                = "native_mtp"
                    owner_agent        = "androidagent"
                    owner_agent_label  = "Android Agent"
                    os_family          = "android"
                }
                return
            }
            if ($adapterName -eq "ios_lockdown" -or $adapterName -match 'ios') {
                $iosJob = Join-Path $root "scripts\native_ios_job.ps1"
                $payloadFile = Join-Path $env:TEMP ("acq_ios_" + $stamp + ".json")
                $tmpOut = Join-Path $env:TEMP ("acq_ios_" + $stamp + ".out")
                $tmpErr = Join-Path $env:TEMP ("acq_ios_" + $stamp + ".err")
                [System.IO.File]::WriteAllText($payloadFile, ($runPayload | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
                $script:Busy = $true
                $script:AcquireBusy = $true
                $proc = Start-Process -FilePath "powershell.exe" -ArgumentList @(
                    "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $iosJob,
                    "-PayloadFile", $payloadFile, "-ResultFile", $tmpResult
                ) -WorkingDirectory $root -WindowStyle Minimized -PassThru -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
                Write-AcquireJobSidecar -JobId $stamp -ProcessId $proc.Id -ResultFile $tmpResult -ProgressFile ([string]$runPayload.progress_file) -CaseId ([string]$runPayload.case_id)
                $script:AcquireJobs[$stamp] = @{
                    Pid = $proc.Id; ResultFile = $tmpResult; ProgressFile = [string]$runPayload.progress_file
                    ErrFile = $tmpErr; OutFile = $tmpOut; Status = "running"; Error = ""; StartedUtc = (Get-Date).ToUniversalTime().ToString("o")
                }
                Write-JsonResponse -Response $response -StatusCode 202 -Body @{
                    ok = $true; async = $true; job_id = $stamp; status = "running"; busy = $true
                    progress = [string]$runPayload.progress_file; via = "native_ios"
                    owner_agent = "iosagent"; owner_agent_label = "iOS Agent"; os_family = "ios"
                }
                return
            }
            $py = Get-HostPython
            if (-not $py) {
                throw "Examiner kit Python is not available. Restart scripts\ensure-host-drive-helper.ps1 -ForceRestart and retry."
            }
            $pt = Join-Path $root "tools\platform-tools"
            $lid = Join-Path $root "tools\libimobiledevice"
            $extraPath = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools"
            $env:PATH = ($pt + ";" + $lid + ";" + $extraPath + ";" + $env:PATH)
            $env:PYTHONPATH = $backend
            $tmpPy = Join-Path $env:TEMP ("acq_run_" + $stamp + ".py")
            $tmpJson = Join-Path $env:TEMP ("acq_run_" + $stamp + ".json")
            $tmpOut = Join-Path $env:TEMP ("acq_run_" + $stamp + ".out")
            $tmpErr = Join-Path $env:TEMP ("acq_run_" + $stamp + ".err")
            [System.IO.File]::WriteAllText($tmpJson, ($runPayload | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
            $pyLines = @(
                'import json, sys, traceback'
                'from pathlib import Path'
                'from app.services.mobile_acquire import AcquisitionRequest, CollectionMethod, CollectionOrchestrator, DeviceDetector'
                'payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))'
                'override = payload.get("method_override") or None'
                'progress_file = payload.get("progress_file") or ""'
                'result_file = payload.get("result_file") or ""'
                'def _write_progress(event):'
                '    if not progress_file: return'
                '    try: Path(progress_file).write_text(json.dumps(event, default=str), encoding="utf-8")'
                '    except Exception: pass'
                'def _write_result(obj):'
                '    text = json.dumps(obj, default=str)'
                '    if result_file:'
                '        try: Path(result_file).write_text(text, encoding="utf-8")'
                '        except Exception: pass'
                '    print(text, flush=True)'
                'def _on_progress(event):'
                '    if isinstance(event, dict): _write_progress(event)'
                'cancel_file = (progress_file or "") + ".cancel"'
                'def _cancelled():'
                '    try: return bool(cancel_file and Path(cancel_file).exists())'
                '    except Exception: return False'
                'req = AcquisitionRequest('
                '    case_id=payload["case_id"], evidence_id=payload["evidence_id"],'
                '    examiner=payload.get("examiner") or "host-acquire", legal_authority=payload["legal_authority"],'
                '    case_root=payload["case_root"], adapter_name=payload["adapter"], device_id=payload["device_id"],'
                '    objective_methods=[CollectionMethod(m) for m in (payload.get("objective_methods") or []) if m],'
                '    authorized_methods=[CollectionMethod(m) for m in (payload.get("authorized_methods") or []) if m],'
                '    examiner_method_override=CollectionMethod(override) if override else None,'
                '    cable_adapter_asset_id=payload.get("cable_adapter_asset_id") or "",'
                '    license_endpoint_id=payload.get("license_endpoint_id") or "",'
                '    backup_password=payload.get("backup_password"),'
                '    examiner_notes=payload.get("examiner_notes") or "",'
                '    device_condition=payload.get("device_condition") or "",'
                '    network_isolated=payload.get("network_isolated"),'
                '    create_working_copy=bool(payload.get("create_working_copy", True)),'
                '    cloud_mailboxes=list(payload.get("cloud_mailboxes") or []),'
                '    authorized_actions=list(payload.get("authorized_actions") or []),'
                '    whatsapp_64_digit_key=payload.get("whatsapp_64_digit_key") or "",'
                '    intake_key_candidates=list(payload.get("intake_key_candidates") or []),'
                ')'
                '_write_progress({"stage":"acquire","item":"host_usb_bridge","bytes_done":0,"detail":"Host USB bridge connected","category":"Preparing"})'
                'try:'
                '    result = CollectionOrchestrator(DeviceDetector(), progress=_on_progress, cancel=_cancelled).run(req)'
                '    try: _final_bytes = int((result.acquisition_record or {}).get("output_size") or 0)'
                '    except Exception: _final_bytes = 0'
                '    _write_progress({"stage": result.stage_reached or "complete", "item":"complete", "bytes_done": _final_bytes, "run_name": result.run_name, "detail":"Collection finished", "category":"Complete"})'
                '    _write_result({"ok": result.ok, **result.as_dict()})'
                'except Exception as exc:'
                '    traceback.print_exc()'
                '    _write_result({"ok": False, "error": str(exc), "errors": [str(exc)], "stage_reached": "failed", "method_decision": {"selected": None, "considered": [], "rejected": {}, "rationale": "Host acquisition failed before method selection completed.", "deeper_available": []}, "limitations": ["Host USB acquisition failed."], "evidence_package": {"run_name": "", "extraction_data": [], "complete": False}, "acquisition_record": {"output_size": 0, "missing_fields": []}, "paths": {}, "chain_of_custody": []})'
                '    sys.exit(1)'
            )
            [System.IO.File]::WriteAllText($tmpPy, ($pyLines -join "`n"), [System.Text.UTF8Encoding]::new($false))
            $script:Busy = $true
            $script:AcquireBusy = $true
            $proc = Start-Process -FilePath $py -ArgumentList @($tmpPy, $tmpJson) -WorkingDirectory $backend -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr -NoNewWindow -PassThru
            $script:AcquireJobs[$stamp] = @{
                Pid          = $proc.Id
                ResultFile   = $tmpResult
                ProgressFile = [string]$runPayload.progress_file
                ErrFile      = $tmpErr
                OutFile      = $tmpOut
                Status       = "running"
                Error        = ""
                StartedUtc   = (Get-Date).ToUniversalTime().ToString("o")
            }
            Write-JsonResponse -Response $response -StatusCode 202 -Body @{
                ok       = $true
                async    = $true
                job_id   = $stamp
                status   = "running"
                busy     = $true
                progress = [string]$runPayload.progress_file
            }
        } catch {
            $script:Busy = $false
            $script:AcquireBusy = $false
            $msg = [string]$_.Exception.Message
            Write-JsonResponse -Response $response -StatusCode 500 -Body @{
                ok = $false; error = $msg; errors = @($msg); stage_reached = "failed"
                method_decision = @{ selected = $null; considered = @(); rejected = @{}; rationale = "Host acquisition failed before method selection completed."; deeper_available = @() }
                limitations = @("Host USB acquisition failed.")
                evidence_package = @{ run_name = ""; extraction_data = @(); complete = $false }
                acquisition_record = @{ output_size = 0; missing_fields = @() }
                paths = @{}; chain_of_custody = @()
            }
        }
        return
    }
    if ($request.HttpMethod -eq "POST" -and ($path -eq "/resolve-folder" -or $path -eq "/resolve_folder")) {
        try {
            $payload = Read-RequestJson -Request $request
            $filenames = @()
            if ($payload.filenames) { $filenames = @($payload.filenames) }
            elseif ($payload.files) { $filenames = @($payload.files) }
            $relativePaths = @()
            if ($payload.relative_paths) { $relativePaths = @($payload.relative_paths) }
            elseif ($payload.paths) { $relativePaths = @($payload.paths) }
            $folderHint = ""
            if ($payload.folder_hint) { $folderHint = [string]$payload.folder_hint }
            elseif ($payload.folder) { $folderHint = [string]$payload.folder }
            elseif ($payload.relative_path) { $folderHint = [string]$payload.relative_path }
            $result = Invoke-ResolveFolder -Filenames $filenames -FolderHint $folderHint -RelativePaths $relativePaths
            Write-JsonResponse -Response $response -StatusCode 200 -Body $result
        }
        catch {
            Write-JsonResponse -Response $response -StatusCode 500 -Body @{
                ok    = $false
                path  = $null
                error = $_.Exception.Message
            }
        }
        return
    }

    if ($request.HttpMethod -eq "POST" -and ($path -eq "/list-dir" -or $path -eq "/list_dir")) {
        try {
            $payload = Read-RequestJson -Request $request
            $listPath = ""
            if ($payload.path) { $listPath = [string]$payload.path }
            elseif ($payload.folder) { $listPath = [string]$payload.folder }
            $result = Invoke-ListDir -Path $listPath
            Write-JsonResponse -Response $response -StatusCode 200 -Body $result
        }
        catch {
            Write-JsonResponse -Response $response -StatusCode 400 -Body @{
                ok      = $false
                path    = $null
                count   = 0
                entries = @()
                error   = $_.Exception.Message
            }
        }
        return
    }

    if ($request.HttpMethod -eq "POST" -and ($path -eq "/stage-folder" -or $path -eq "/stage_folder")) {
        try {
            $payload = Read-RequestJson -Request $request
            $srcPath = ""
            if ($payload.path) { $srcPath = [string]$payload.path }
            elseif ($payload.folder) { $srcPath = [string]$payload.folder }
            $jobId = ""
            if ($payload.job_id) { $jobId = [string]$payload.job_id }
            elseif ($payload.jobId) { $jobId = [string]$payload.jobId }
            $result = Invoke-StageFolder -Path $srcPath -JobId $jobId
            Write-JsonResponse -Response $response -StatusCode 200 -Body $result
        }
        catch {
            Write-JsonResponse -Response $response -StatusCode 400 -Body @{
                ok     = $false
                path   = $null
                dest   = $null
                error  = $_.Exception.Message
            }
        }
        return
    }

    if ($request.HttpMethod -eq "GET" -and ($path -eq "/refresh-drive-mounts" -or $path -eq "/refresh-drive-mounts/status")) {
        $state = Get-RefreshDriveMountsStatus
        Write-JsonResponse -Response $response -StatusCode 200 -Body $state
        return
    }

    if ($request.HttpMethod -eq "POST" -and $path -eq "/refresh-drive-mounts") {
        try {
            $payload = Read-RequestJson -Request $request
            $requiredPath = ""
            if ($payload.required_path) { $requiredPath = [string]$payload.required_path }
            elseif ($payload.path) { $requiredPath = [string]$payload.path }
            $result = Start-RefreshDriveMountsAsync -RequiredPath $requiredPath
            Write-JsonResponse -Response $response -StatusCode 202 -Body $result
        }
        catch {
            $statusCode = 500
            if ($_.Exception.Message -match 'already in progress') {
                $statusCode = 409
            }
            elseif ($_.Exception.Message -match 'absolute Windows drive path|Windows cannot see') {
                $statusCode = 400
            }
            Write-JsonResponse -Response $response -StatusCode $statusCode -Body @{
                ok    = $false
                error = $_.Exception.Message
            }
        }
        return
    }

    if ($request.HttpMethod -eq "POST" -and ($path -eq "/acquire" -or $path -eq "/mobile-acquire")) {
        if (-not (Get-Command Invoke-MobileAcquire -ErrorAction SilentlyContinue)) {
            Write-JsonResponse -Response $response -StatusCode 500 -Body @{
                ok    = $false
                error = "Mobile acquire script not loaded (scripts/host-mobile-acquire.ps1)"
            }
            return
        }
        if (Test-UsbAcquireInProgress) {
            Write-UsbBusyResponse -Response $response
            return
        }
        $script:Busy = $true
        $script:AcquireBusy = $true
        try {
            $payload = Read-RequestJson -Request $request
            $jobId = ""
            if ($payload.job_id) { $jobId = [string]$payload.job_id }
            elseif ($payload.mobile_job_id) { $jobId = [string]$payload.mobile_job_id }
            if (-not $jobId) { throw "job_id is required" }

            $deviceId = ""
            if ($payload.device_id) { $deviceId = [string]$payload.device_id }
            elseif ($payload.id) { $deviceId = [string]$payload.id }

            $deviceName = ""
            if ($payload.device_name) { $deviceName = [string]$payload.device_name }
            elseif ($payload.name) { $deviceName = [string]$payload.name }

            $osHint = "other"
            if ($payload.os_hint) { $osHint = [string]$payload.os_hint }
            elseif ($payload.mobile_os) { $osHint = [string]$payload.mobile_os }

            $instanceId = ""
            if ($payload.instance_id) { $instanceId = [string]$payload.instance_id }

            $outputRoot = ""
            if ($payload.output_root) { $outputRoot = [string]$payload.output_root }

            if (-not $deviceName) {
                $detected = @(Get-ConnectedMobileDevices)
                if ($detected.Count -gt 0) {
                    $match = $detected | Where-Object { $_.id -eq $deviceId } | Select-Object -First 1
                    if (-not $match) { $match = $detected[0] }
                    $deviceName = [string]$match.name
                    if (-not $deviceId) { $deviceId = [string]$match.id }
                    if (-not $osHint -or $osHint -eq "other") { $osHint = [string]$match.os_hint }
                    if (-not $instanceId) { $instanceId = [string]$match.instance_id }
                }
            }

            $result = Invoke-MobileAcquire `
                -JobId $jobId `
                -DeviceId $deviceId `
                -DeviceName $deviceName `
                -OsHint $osHint `
                -InstanceId $instanceId `
                -OutputRoot $outputRoot
            Write-JsonResponse -Response $response -StatusCode 200 -Body $result
        }
        catch {
            Write-JsonResponse -Response $response -StatusCode 500 -Body @{
                ok    = $false
                error = $_.Exception.Message
            }
        }
        finally {
            $script:Busy = $false
            $script:AcquireBusy = $false
        }
        return
    }

    Write-JsonResponse -Response $response -StatusCode 404 -Body @{
        ok    = $false
        error = "Not found"
    }
}

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Error "PowerShell 5.1 or later is required."
}

$listener = New-Object System.Net.HttpListener
$listenPrefix = "http://$BindHost`:$Port/"
$listener.Prefixes.Add($listenPrefix)
try {
    $listener.Start()
}
catch {
    if ($BindHost -eq "+") {
        throw "Could not bind $listenPrefix. Run scripts\configure-host-drive-helper-network.ps1 as Administrator once, then restart the helper. $($_.Exception.Message)"
    }
    throw
}
Start-DrivePresenceWatcher
Write-HostAdapterCache
Write-MobileDeviceCaches
Start-MobileDeviceRefresh

Add-Type -TypeDefinition @"
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;
// V1.31.9: the phone list is what Windows has plugged in at the moment of the request.
// Before, /acquisition/devices and /mobile-devices returned a file written by a background job every
// 10-15 s. When that job stopped writing, the page kept showing phones that were unplugged long ago
// ("ADB Device" with nothing attached) and never showed a newly plugged iPhone. Now each request reads the
// present USB devices from Windows (SetupAPI, the same list Device Manager shows: no USB traffic, no
// pnputil, no WMI, a few milliseconds) and classifies them here. The background file is used only if
// this read fails.
public class AetherisUsbNode {
  public string Id = "";
  public string Desc = "";
  public string Mfg = "";
  public string Ids = "";
  public bool Started = true;
  public int Problem = 0;
}

public class AetherisPhone {
  public string Key = "";
  public string Os = "";
  public string Connection = "";
  public string Adapter = "";
  public string Status = "";
  public string Id = "";
  public string Name = "";
  public string InstanceId = "";
  public string State = "";
  public string Guidance = "";
  public string DeviceClass = "";
  public string Capability = "";
  public bool LiveAcquire = true;
}

public static class AetherisUsbLive {
  [StructLayout(LayoutKind.Sequential)]
  struct SP_DEVINFO_DATA { public int cbSize; public Guid ClassGuid; public int DevInst; public IntPtr Reserved; }
  [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern IntPtr SetupDiGetClassDevsW(IntPtr classGuid, string enumerator, IntPtr hwndParent, int flags);
  [DllImport("setupapi.dll", SetLastError = true)]
  static extern bool SetupDiEnumDeviceInfo(IntPtr deviceInfoSet, int memberIndex, ref SP_DEVINFO_DATA deviceInfoData);
  [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool SetupDiGetDeviceInstanceIdW(IntPtr deviceInfoSet, ref SP_DEVINFO_DATA deviceInfoData, StringBuilder id, int size, out int required);
  [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool SetupDiGetDeviceRegistryPropertyW(IntPtr deviceInfoSet, ref SP_DEVINFO_DATA deviceInfoData, int property, out int regType, byte[] buffer, int size, out int required);
  [DllImport("setupapi.dll", SetLastError = true)]
  static extern bool SetupDiDestroyDeviceInfoList(IntPtr deviceInfoSet);
  [DllImport("cfgmgr32.dll")]
  static extern int CM_Get_DevNode_Status(out int status, out int problem, int devInst, int flags);

  const int DIGCF_PRESENT = 0x2;
  const int DIGCF_ALLCLASSES = 0x4;
  const int SPDRP_DEVICEDESC = 0x0;
  const int SPDRP_HARDWAREID = 0x1;
  const int SPDRP_COMPATIBLEIDS = 0x2;
  const int SPDRP_MFG = 0xB;
  const int SPDRP_FRIENDLYNAME = 0xC;
  const int DN_STARTED = 0x8;
  const int DN_HAS_PROBLEM = 0x400;

  /// Where the present USB devices come from (tests replace it).
  public static Func<List<AetherisUsbNode>> Source = ScanSetupApi;
  public static int CacheMilliseconds = 1500;
  static readonly object Gate = new object();
  static List<AetherisPhone> _last;
  static DateTime _lastAt = DateTime.MinValue;
  static string _lastError = "";
  public static string LastError { get { return _lastError; } }

  public static List<AetherisPhone> Phones(out bool ok) {
    lock (Gate) {
      if (_last != null && (DateTime.UtcNow - _lastAt).TotalMilliseconds < CacheMilliseconds) { ok = true; return _last; }
      try {
        List<AetherisPhone> phones = Classify(Source());
        _last = phones;
        _lastAt = DateTime.UtcNow;
        _lastError = "";
        ok = true;
        return phones;
      } catch (Exception ex) {
        _lastError = ex.Message;
        _last = null;
        ok = false;
        return new List<AetherisPhone>();
      }
    }
  }

  public static void Reset() { lock (Gate) { _last = null; _lastAt = DateTime.MinValue; } }

  static string Prop(IntPtr set, ref SP_DEVINFO_DATA data, int property) {
    int regType;
    int required;
    byte[] buffer = new byte[2048];
    if (!SetupDiGetDeviceRegistryPropertyW(set, ref data, property, out regType, buffer, buffer.Length, out required)) {
      if (required <= buffer.Length || required > 65536) return "";
      buffer = new byte[required];
      if (!SetupDiGetDeviceRegistryPropertyW(set, ref data, property, out regType, buffer, buffer.Length, out required)) return "";
    }
    string text = Encoding.Unicode.GetString(buffer, 0, Math.Min(required, buffer.Length));
    return string.Join(" ", text.Split(new char[] { '\0' }, StringSplitOptions.RemoveEmptyEntries)).Trim();
  }

  public static List<AetherisUsbNode> ScanSetupApi() {
    List<AetherisUsbNode> nodes = new List<AetherisUsbNode>();
    IntPtr set = SetupDiGetClassDevsW(IntPtr.Zero, "USB", IntPtr.Zero, DIGCF_PRESENT | DIGCF_ALLCLASSES);
    if (set == IntPtr.Zero || set == new IntPtr(-1)) throw new InvalidOperationException("SetupDiGetClassDevs failed: " + Marshal.GetLastWin32Error());
    try {
      for (int i = 0; i < 4096; i++) {
        SP_DEVINFO_DATA data = new SP_DEVINFO_DATA();
        data.cbSize = Marshal.SizeOf(typeof(SP_DEVINFO_DATA));
        if (!SetupDiEnumDeviceInfo(set, i, ref data)) break;
        StringBuilder id = new StringBuilder(512);
        int required;
        if (!SetupDiGetDeviceInstanceIdW(set, ref data, id, id.Capacity, out required)) continue;
        string instance = id.ToString();
        if (instance.IndexOf("VID_", StringComparison.OrdinalIgnoreCase) < 0) continue;
        AetherisUsbNode node = new AetherisUsbNode();
        node.Id = instance;
        string friendly = Prop(set, ref data, SPDRP_FRIENDLYNAME);
        node.Desc = friendly.Length > 0 ? friendly : Prop(set, ref data, SPDRP_DEVICEDESC);
        node.Mfg = Prop(set, ref data, SPDRP_MFG);
        node.Ids = Prop(set, ref data, SPDRP_HARDWAREID) + " " + Prop(set, ref data, SPDRP_COMPATIBLEIDS);
        int status;
        int problem;
        if (CM_Get_DevNode_Status(out status, out problem, data.DevInst, 0) == 0) {
          node.Started = (status & DN_STARTED) != 0;
          node.Problem = (status & DN_HAS_PROBLEM) != 0 ? Math.Max(problem, 1) : 0;
        }
        nodes.Add(node);
      }
    } finally {
      SetupDiDestroyDeviceInfoList(set);
    }
    return nodes;
  }

  // Same rules as Get-AndroidUsbStates in host-drive-helper.ps1 (V1.31.7).
  static readonly Regex IdRe = new Regex(@"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(&MI_[0-9A-F]{2})?\\(.+)\z", RegexOptions.IgnoreCase);
  static readonly string[] PhoneVendors = new string[] { "18D1", "2717", "2A70", "22D9", "2D95", "22B8", "339B", "2A45", "2E04", "29A9", "2B4C" };
  static readonly string[] MaybeVendors = new string[] { "04E8", "0FCE", "1004", "0B05", "0E8D", "05C6", "12D1", "19D2", "0BB4" };
  static readonly Regex PhoneWords = new Regex("android|phone|galaxy|pixel|redmi|xiaomi|poco|oneplus|oppo|vivo|realme|iqoo|moto|nokia|honor|huawei|nothing|infinix|tecno|itel|xperia|mobile", RegexOptions.IgnoreCase);
  static readonly Regex NotPhoneWords = new Regex("modem|network|broadband|mobile connect|card reader|keyboard|mouse|headset|webcam|printer|bluetooth|mass storage|ssd|flash drive", RegexOptions.IgnoreCase);
  static readonly Regex GenericDesc = new Regex(@"^(USB Composite Device|USB Device|WinUsb Device|ADB Interface|ADB Device|Android ADB Interface|Android Composite ADB Interface|MTP USB Device|USB Input Device|Generic USB Hub|USB Mass Storage Device)\z", RegexOptions.IgnoreCase);
  static readonly Regex FallbackDesc = new Regex(@"(Composite Device|ADB Interface|Modem)\z", RegexOptions.IgnoreCase);
  static readonly Regex AdbRe = new Regex("SubClass_42&Prot_01|MS_COMP_ADB", RegexOptions.IgnoreCase);
  static readonly Regex MsMtpRe = new Regex("MS_COMP_MTP", RegexOptions.IgnoreCase);
  static readonly Regex PtpRe = new Regex("Class_06&SubClass_01&Prot_01", RegexOptions.IgnoreCase);
  static readonly Regex AppleNameRe = new Regex("iPhone|iPad|iPod", RegexOptions.IgnoreCase);

  public const string ChargingOnlyGuidance = "Connected for charging only. Unlock the phone, open the USB notification on it and choose File transfer (for a deeper collection also turn on USB debugging and tap Allow), then Rescan.";
  public const string DriverMissingGuidance = "Windows has no working driver for this phone's USB interface (Device Manager shows it with a warning). On the phone choose USB: File transfer, which needs no extra driver, or install the phone maker's USB driver, then Rescan.";

  class Group {
    public string Vid = "";
    public string Pid = "";
    public string Serial = "";
    public string Instance = "";
    public string Desc = "";
    public string MtpDesc = "";
    public string FallbackDesc = "";
    public string AppleName = "";
    public bool Phone;
    public bool AdbOk;
    public bool AdbBad;
    public bool MtpOk;
    public bool MtpBad;
    public bool MsMtp;
  }

  static bool Contains(string[] list, string value) { return Array.IndexOf(list, value) >= 0; }

  public static List<AetherisPhone> Classify(List<AetherisUsbNode> nodes) {
    Dictionary<string, Group> groups = new Dictionary<string, Group>(StringComparer.OrdinalIgnoreCase);
    List<string> order = new List<string>();
    foreach (AetherisUsbNode node in nodes ?? new List<AetherisUsbNode>()) {
      Match m = IdRe.Match(node.Id ?? "");
      if (!m.Success) continue;
      string vid = m.Groups[1].Value.ToUpperInvariant();
      string pid = m.Groups[2].Value.ToUpperInvariant();
      bool iface = m.Groups[3].Success;
      string tail = m.Groups[4].Value;
      string key = vid + "|" + pid;
      Group g;
      if (!groups.TryGetValue(key, out g)) { g = new Group(); g.Vid = vid; g.Pid = pid; groups[key] = g; order.Add(key); }
      string ids = node.Ids ?? "";
      string desc = (node.Desc ?? "").Trim();
      bool started = node.Started && node.Problem == 0;
      bool isAdb = AdbRe.IsMatch(ids);
      bool msMtp = MsMtpRe.IsMatch(ids);
      bool isMtp = msMtp || PtpRe.IsMatch(ids);
      if (isAdb) { if (started) g.AdbOk = true; else g.AdbBad = true; }
      if (isMtp) { if (started) g.MtpOk = true; else g.MtpBad = true; }
      if (msMtp) g.MsMtp = true;
      if (!iface && tail.IndexOf('&') < 0) { g.Serial = tail; g.Instance = node.Id; }
      if (g.Instance.Length == 0) g.Instance = node.Id;
      if (vid == "05AC") {
        if (g.AppleName.Length == 0 && AppleNameRe.IsMatch(desc)) g.AppleName = desc;
        continue;
      }
      string words = desc + " " + (node.Mfg ?? "");
      if (isAdb || Contains(PhoneVendors, vid) ||
          ((Contains(MaybeVendors, vid) || isMtp) && PhoneWords.IsMatch(words) && !NotPhoneWords.IsMatch(words))) g.Phone = true;
      if (desc.Length > 0 && !GenericDesc.IsMatch(desc)) {
        if (isMtp && g.MtpDesc.Length == 0) g.MtpDesc = desc;
        if (FallbackDesc.IsMatch(desc)) { if (g.FallbackDesc.Length == 0) g.FallbackDesc = desc; }
        else if (g.Desc.Length == 0) g.Desc = desc;
      }
    }

    List<AetherisPhone> phones = new List<AetherisPhone>();
    foreach (string key in order) {
      Group g = groups[key];
      AetherisPhone p = new AetherisPhone();
      p.Key = key;
      p.InstanceId = g.Instance;
      if (g.Vid == "05AC") {
        int pid = Convert.ToInt32(g.Pid, 16);
        // iPhone / iPad / iPod in normal mode use product IDs 0x1290-0x12FF; other Apple USB devices
        // (keyboards, adapters, displays) do not, and are not listed.
        if (!(pid >= 0x1290 && pid <= 0x12FF) && g.AppleName.Length == 0) continue;
        p.Os = "ios";
        p.Connection = "usb";
        p.Adapter = "ios_lockdown";
        p.Status = "usb";
        p.Id = g.Instance;
        p.Name = g.AppleName.Length > 0 ? g.AppleName : "Apple iPhone";
        p.DeviceClass = "USB";
        p.Capability = "SUPPORTED_DIRECT_LOGICAL";
        p.Guidance = "Unlock the iPhone and tap Trust when asked, then Identify.";
        phones.Add(p);
        continue;
      }
      if (!g.Phone && !(g.MtpOk && g.MsMtp)) continue;
      p.Os = "android";
      if (g.AdbOk) {
        p.Connection = "adb";
        p.Adapter = "android_adb";
        p.Status = "adb";
        p.Id = g.Serial.Length > 0 ? g.Serial : g.Instance;
        p.Name = g.MtpDesc.Length > 0 ? g.MtpDesc : (g.Desc.Length > 0 ? g.Desc : "Android phone");
        p.DeviceClass = "ADB";
        p.Capability = "SUPPORTED_DIRECT_LOGICAL";
        p.Guidance = "USB debugging is on. Use Identify, then Start extraction.";
      } else if (g.MtpOk) {
        p.Connection = "mtp";
        p.Adapter = "android_mtp";
        p.Status = "wpd";
        p.Id = g.Instance;
        p.Name = g.MtpDesc.Length > 0 ? g.MtpDesc : (g.Desc.Length > 0 ? g.Desc : (g.FallbackDesc.Length > 0 ? g.FallbackDesc : "Android phone (File transfer)"));
        p.DeviceClass = "WPD";
        p.Capability = "SUPPORTED_DIRECT_LOGICAL";
        p.Guidance = "Connected phone detected (File transfer). Use Start extraction.";
      } else {
        p.Connection = "usb";
        p.Adapter = "android_usb";
        p.State = (g.AdbBad || g.MtpBad) ? "driver_missing" : "charging_only";
        p.Status = p.State;
        p.Id = g.Serial.Length > 0 ? g.Serial : g.Instance;
        p.Name = g.Desc.Length > 0 ? g.Desc : (g.FallbackDesc.Length > 0 ? g.FallbackDesc : "Android phone");
        p.DeviceClass = "USB";
        p.Capability = "NEEDS_ACTION";
        p.Guidance = p.State == "driver_missing" ? DriverMissingGuidance : ChargingOnlyGuidance;
        p.LiveAcquire = false;
      }
      phones.Add(p);
    }
    return phones;
  }

  static string Esc(string s) {
    if (s == null) return "";
    StringBuilder sb = new StringBuilder(s.Length + 8);
    foreach (char c in s) {
      if (c == '\\') sb.Append("\\\\");
      else if (c == '"') sb.Append("\\\"");
      else if (c < (char)0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
      else sb.Append(c);
    }
    return sb.ToString();
  }

  static string Now() { return DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture); }

  public static string AcquisitionJson(List<AetherisPhone> phones) {
    List<string> rows = new List<string>();
    foreach (AetherisPhone p in phones) {
      rows.Add("{\"adapter\":\"" + Esc(p.Adapter) + "\",\"device_id\":\"" + Esc(p.Id) + "\",\"label\":\"" + Esc(p.Name) +
        "\",\"status\":\"" + Esc(p.Status) + "\",\"os_family\":\"" + Esc(p.Os) + "\",\"instance_id\":\"" + Esc(p.InstanceId) +
        "\",\"connection\":\"" + Esc(p.Connection) + "\",\"guidance\":\"" + Esc(p.Guidance) + "\",\"state\":\"" + Esc(p.State) + "\"}");
    }
    return "{\"ok\":true,\"devices\":[" + string.Join(",", rows.ToArray()) + "],\"warnings\":[],\"count\":" + rows.Count +
      ",\"scanned_utc\":\"" + Now() + "\",\"source\":\"live\",\"note\":\"\"}";
  }

  public static string MobileJson(List<AetherisPhone> phones) {
    List<string> rows = new List<string>();
    foreach (AetherisPhone p in phones) {
      rows.Add("{\"id\":\"" + Esc(p.Id) + "\",\"name\":\"" + Esc(p.Name) + "\",\"device_class\":\"" + Esc(p.DeviceClass) +
        "\",\"connection\":\"" + Esc(p.Connection) + "\",\"os_hint\":\"" + Esc(p.Os) + "\",\"instance_id\":\"" + Esc(p.InstanceId) +
        "\",\"has_drive_letter\":false,\"live_acquire\":" + (p.LiveAcquire ? "true" : "false") +
        ",\"capability_label\":\"" + Esc(p.Capability) + "\",\"guidance\":\"" + Esc(p.Guidance) + "\",\"state\":\"" + Esc(p.State) +
        "\",\"ios_backup_roots\":[]}");
    }
    return "{\"ok\":true,\"mobile_devices\":[" + string.Join(",", rows.ToArray()) + "],\"count\":" + rows.Count +
      ",\"scanned_utc\":\"" + Now() + "\",\"source\":\"live\"}";
  }
}

public class AetherisHelperDispatcher {
  public readonly BlockingCollection<HttpListenerContext> Queue = new BlockingCollection<HttpListenerContext>();
  HttpListener _listener;
  Thread _thread;
  volatile bool _stop;
  public string HealthJson = "{\"ok\":true,\"service\":\"host-drive-helper\"}";
  public string AdaptersJsonPath = "";
  public string DevicesJsonPath = "";
  public string MobileJsonPath = "";
  public string RootPath = "";
  public string NativeMtpJobScript = "";
  public string NativeIosJobScript = "";
  public string JobsDir = "";
  public string ExpectedToken = "";
  public string RefreshStatusFile = "";
  public string RefreshJobScript = "";
  int _liveBusy;
  int _liveFiles;
  long _liveBytes;
  string _liveNewest = "";
  string _livePath = "";
  public string DefaultAdaptersJson = "{\"ok\":true,\"adapters\":[{\"name\":\"android_mtp\",\"os_family\":\"android\",\"available\":true,\"reason\":\"Windows MTP/WPD ready\",\"via\":\"host\"},{\"name\":\"android_adb\",\"os_family\":\"android\",\"available\":true,\"reason\":\"Host USB helper\",\"via\":\"host\"},{\"name\":\"ios_lockdown\",\"os_family\":\"ios\",\"available\":true,\"reason\":\"Host USB helper\",\"via\":\"host\"}]}";
  public string DefaultDevicesJson = "{\"ok\":true,\"devices\":[],\"warnings\":[],\"count\":0}";
  public void Start(HttpListener listener) {
    _listener = listener;
    _thread = new Thread(AcceptLoop);
    _thread.IsBackground = true;
    _thread.Name = "aetheris-helper-accept";
    _thread.Start();
  }
  void AcceptLoop() {
    while (!_stop && _listener != null && _listener.IsListening) {
      try {
        HttpListenerContext ctx = _listener.GetContext();
        string path = "";
        string query = "";
        if (ctx.Request.Url != null) {
          path = ctx.Request.Url.AbsolutePath.TrimEnd('/');
          query = ctx.Request.Url.Query ?? "";
        }
        string method = ctx.Request.HttpMethod ?? "";
        if (method == "OPTIONS") WriteCors(ctx);
        else if (path == "" || path == "/" || path == "/health") WriteHealth(ctx);
        else if (method == "POST" && path == "/acquisition/preview") WriteNativePreview(ctx);
        else if (method == "POST" && path.StartsWith("/acquisition/import/", StringComparison.Ordinal)) {
          HttpListenerContext importCtx = ctx;
          string importPath = path;
          ThreadPool.QueueUserWorkItem(delegate(object state) { AetherisImport.Handle(importCtx, importPath); });
        }
        else if (method == "POST" && path == "/acquisition/run" &&
                 query.IndexOf("native=", StringComparison.OrdinalIgnoreCase) >= 0) WriteNativeUsbRun(ctx, query);
        else if (method == "POST" && path == "/acquisition/cancel") WriteCancelUsb(ctx);
        else if (method == "GET" && path == "/acquisition/jobs") WriteJobList(ctx);
        else if (method == "GET" && (path == "/acquisition/job" || path.StartsWith("/acquisition/job/"))) {
          if (!WriteJobStatus(ctx, path, query)) Queue.Add(ctx);
        }
        else if (method == "GET" && path == "/acquisition/adapters") WriteCached(ctx, AdaptersJsonPath, DefaultAdaptersJson);
        else if (method == "GET" && path == "/acquisition/devices") WriteLiveDevices(ctx, query);
        else if (method == "GET" && (path == "/mobile-devices" || path == "/mobile_devices")) WriteLiveMobile(ctx, query);
        else if (method == "GET" && path == "/drives") WriteDrives(ctx);
        else if (method == "GET" && (path == "/refresh-drive-mounts" || path == "/refresh-drive-mounts/status")) WriteRefreshStatus(ctx);
        else if (method == "POST" && path == "/refresh-drive-mounts") StartRefreshMount(ctx);
        else Queue.Add(ctx);
      } catch (HttpListenerException) { break; }
      catch (ObjectDisposedException) { break; }
      catch { }
    }
  }
  static string JsonStr(string json, string key) {
    if (string.IsNullOrEmpty(json) || string.IsNullOrEmpty(key)) return "";
    string needle = "\"" + key + "\"";
    int i = json.IndexOf(needle, StringComparison.OrdinalIgnoreCase);
    if (i < 0) return "";
    i = json.IndexOf(':', i + needle.Length);
    if (i < 0) return "";
    i++;
    while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
    if (i >= json.Length) return "";
    if (json[i] == 'n' && json.IndexOf("null", i, StringComparison.OrdinalIgnoreCase) == i) return "";
    if (json[i] == 't' || json[i] == 'f' || json[i] == '{' || json[i] == '[') return "";
    if (json[i] != '"') return "";
    StringBuilder sb = new StringBuilder();
    for (int j = i + 1; j < json.Length; j++) {
      char c = json[j];
      if (c == '\\' && j + 1 < json.Length) { sb.Append(json[++j]); continue; }
      if (c == '"') break;
      sb.Append(c);
    }
    return sb.ToString();
  }
  static string JsonEscape(string value) {
    if (string.IsNullOrEmpty(value)) return "";
    return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
  }
  void WriteCors(HttpListenerContext ctx) {
    try {
      HttpListenerResponse res = ctx.Response;
      res.StatusCode = 204;
      res.AddHeader("Access-Control-Allow-Origin", "*");
      res.AddHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
      res.AddHeader("Access-Control-Allow-Headers", "Content-Type, X-Host-Helper-Token");
      res.ContentLength64 = 0;
      res.OutputStream.Close();
    } catch {}
  }
  void WriteJson(HttpListenerContext ctx, int status, string json) {
    try {
      byte[] bytes = Encoding.UTF8.GetBytes(json ?? "{}");
      HttpListenerResponse res = ctx.Response;
      res.StatusCode = status;
      res.ContentType = "application/json; charset=utf-8";
      res.AddHeader("Access-Control-Allow-Origin", "*");
      res.ContentLength64 = bytes.Length;
      res.OutputStream.Write(bytes, 0, bytes.Length);
      res.OutputStream.Close();
    } catch {}
  }
  void WriteBytes(HttpListenerContext ctx, string json) {
    WriteJson(ctx, 200, json);
  }
  // V1.31.8: the page asks here whether the Client Evidence Agent (port 9877) is running, instead of
  // calling 127.0.0.1:9877 itself: each refused browser call shows up as a red console error.
  public int ClientAgentPort = 9877;
  readonly object _agentLock = new object();
  DateTime _agentCheckedUtc = DateTime.MinValue;
  bool _agentUp;
  bool ClientAgentListening() {
    lock (_agentLock) {
      if ((DateTime.UtcNow - _agentCheckedUtc).TotalSeconds < 3) return _agentUp;
    }
    bool up = false;
    try {
      using (System.Net.Sockets.TcpClient c = new System.Net.Sockets.TcpClient()) {
        IAsyncResult ar = c.BeginConnect("127.0.0.1", ClientAgentPort, null, null);
        if (ar.AsyncWaitHandle.WaitOne(250) && c.Connected) { c.EndConnect(ar); up = true; }
      }
    } catch { up = false; }
    lock (_agentLock) { _agentUp = up; _agentCheckedUtc = DateTime.UtcNow; }
    return up;
  }
  void WriteHealth(HttpListenerContext ctx) {
    string json = HealthJson ?? "{\"ok\":true}";
    try {
      int end = json.LastIndexOf('}');
      if (end > 0) json = json.Substring(0, end) + ",\"client_agent_running\":" + (ClientAgentListening() ? "true" : "false") + "}";
    } catch {}
    WriteBytes(ctx, json);
  }
  void WriteCached(HttpListenerContext ctx, string path, string fallback) {
    string json = fallback;
    try {
      if (!string.IsNullOrEmpty(path) && File.Exists(path)) {
        string raw = File.ReadAllText(path);
        if (!string.IsNullOrWhiteSpace(raw)) json = raw;
      }
    } catch {}
    WriteBytes(ctx, json);
  }
  // V1.10.10: GET /drives is answered here, on the accept thread, from the
  // Windows drive table (no WMI / Storage cmdlets, no PowerShell queue). Before,
  // it waited behind any long request (folder listing, drive refresh) and ran
  // Get-Volume + Get-Partition + Win32_LogicalDisk, so the server's 4 s timeout
  // fell back to the drives Docker mounted at startup and a newly attached
  // USB / external disk (e.g. G:) never appeared.
  static string JsonEsc(string s) {
    if (s == null) return "";
    StringBuilder sb = new StringBuilder(s.Length + 8);
    foreach (char c in s) {
      if (c == '\\') sb.Append("\\\\");
      else if (c == '"') sb.Append("\\\"");
      else if (c < (char)0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
      else sb.Append(c);
    }
    return sb.ToString();
  }
  public static string DriveRow(string root) {
    string letter = root.Substring(0, 1).ToUpperInvariant();
    string type = "other";
    string label = "";
    string fs = "";
    long size = 0;
    long free = 0;
    bool ready = false;
    try {
      DriveInfo d = new DriveInfo(root);
      DriveType t = d.DriveType;
      if (t == DriveType.Fixed || t == DriveType.Ram) type = "fixed";
      else if (t == DriveType.Removable) type = "removable";
      else if (t == DriveType.Network) type = "network";
      else if (t == DriveType.CDRom) type = "cdrom";
      // A disconnected mapped share can block on any volume query: list its
      // letter without touching it.
      if (t != DriveType.Network) {
        ready = d.IsReady;
        if (ready) {
          try { label = d.VolumeLabel; } catch {}
          try { fs = d.DriveFormat; } catch {}
          try { size = d.TotalSize; } catch {}
          try { free = d.TotalFreeSpace; } catch {}
        } else if (t == DriveType.Removable || t == DriveType.CDRom) {
          return null;  // empty card-reader slot / no disc: Explorer hides these too
        }
      }
    } catch {}
    return "{\"letter\":\"" + letter + "\",\"label\":\"" + JsonEsc(label) + "\",\"drive_type\":\"" + type +
      "\",\"size_bytes\":" + size + ",\"free_bytes\":" + free + ",\"filesystem\":\"" + JsonEsc(fs) +
      "\",\"ready\":" + (ready ? "true" : "false") + "}";
  }
  static int CountTopLevelObjects(string jsonArray) {
    int depth = 0, count = 0;
    bool inString = false, escaped = false;
    foreach (char c in jsonArray) {
      if (inString) {
        if (escaped) escaped = false;
        else if (c == '\\') escaped = true;
        else if (c == '"') inString = false;
        continue;
      }
      if (c == '"') inString = true;
      else if (c == '[' || c == '{') { if (c == '{' && depth == 1) count++; depth++; }
      else if (c == ']' || c == '}') depth--;
    }
    return count;
  }
  public static string DrivesJson(string mobileJsonPath, int detailTimeoutMs) {
    Stopwatch sw = Stopwatch.StartNew();
    List<string> roots = new List<string>();
    foreach (string root in Environment.GetLogicalDrives()) {
      if (!string.IsNullOrEmpty(root) && root.Length >= 2 && root[1] == ':' && char.IsLetter(root[0])) roots.Add(root);
    }
    // Volume details in parallel with one time limit: a stuck device cannot hold
    // the answer; it is listed by letter only.
    List<Task<string>> tasks = new List<Task<string>>();
    foreach (string root in roots) {
      string r = root;
      tasks.Add(Task.Factory.StartNew<string>(() => DriveRow(r)));
    }
    try { Task.WaitAll(tasks.ToArray(), detailTimeoutMs); } catch {}
    List<string> volumes = new List<string>();
    List<string> letters = new List<string>();
    for (int i = 0; i < roots.Count; i++) {
      string letter = roots[i].Substring(0, 1).ToUpperInvariant();
      string row;
      if (tasks[i].Status == TaskStatus.RanToCompletion) {
        row = tasks[i].Result;
        if (row == null) continue;
      } else {
        row = "{\"letter\":\"" + letter + "\",\"label\":\"\",\"drive_type\":\"other\",\"size_bytes\":0,\"free_bytes\":0,\"filesystem\":\"\",\"ready\":false,\"slow\":true}";
      }
      volumes.Add(row);
      letters.Add("\"" + letter + "\"");
    }
    string mobile = "[]";
    try {
      if (!string.IsNullOrEmpty(mobileJsonPath) && File.Exists(mobileJsonPath)) {
        string raw = File.ReadAllText(mobileJsonPath).Trim();
        if (raw.StartsWith("[")) mobile = raw;
      }
    } catch {}
    return "{\"ok\":true,\"source\":\"helper_fast\",\"drives\":[" + string.Join(",", letters.ToArray()) +
      "],\"volumes\":[" + string.Join(",", volumes.ToArray()) + "],\"count\":" + volumes.Count +
      ",\"mobile_devices\":" + mobile + ",\"mobile_count\":" + CountTopLevelObjects(mobile) +
      ",\"scanned_ms\":" + sw.ElapsedMilliseconds + "}";
  }
  void WriteDrives(HttpListenerContext ctx) {
    if (!string.IsNullOrEmpty(ExpectedToken) && ctx.Request.Headers["X-Host-Helper-Token"] != ExpectedToken) {
      WriteJson(ctx, 401, "{\"ok\":false,\"error\":\"Unauthorized\"}");
      return;
    }
    string json;
    try { json = DrivesJson(MobileJsonPath, 2500); }
    catch (Exception ex) { json = "{\"ok\":false,\"error\":\"" + JsonEsc(ex.Message) + "\"}"; }
    WriteBytes(ctx, json);
  }
  // V1.31.9: phones present now (AetherisUsbLive). ?cached=1 returns the background job's file instead,
  // which is also the fallback when Windows cannot be asked.
  void WriteLiveDevices(HttpListenerContext ctx, string query) {
    bool ok = false;
    List<AetherisPhone> phones = null;
    if ((query ?? "").IndexOf("cached=1", StringComparison.OrdinalIgnoreCase) < 0) phones = AetherisUsbLive.Phones(out ok);
    if (!ok) { WriteCached(ctx, DevicesJsonPath, DefaultDevicesJson); return; }
    WriteBytes(ctx, AetherisUsbLive.AcquisitionJson(phones));
  }
  void WriteLiveMobile(HttpListenerContext ctx, string query) {
    bool ok = false;
    List<AetherisPhone> phones = null;
    if ((query ?? "").IndexOf("cached=1", StringComparison.OrdinalIgnoreCase) < 0) phones = AetherisUsbLive.Phones(out ok);
    if (!ok) { WriteMobileDevices(ctx); return; }
    WriteBytes(ctx, AetherisUsbLive.MobileJson(phones));
  }
  void WriteMobileDevices(HttpListenerContext ctx) {
    string json = "{\"ok\":true,\"mobile_devices\":[],\"count\":0}";
    try {
      if (!string.IsNullOrEmpty(MobileJsonPath) && File.Exists(MobileJsonPath)) {
        string raw = File.ReadAllText(MobileJsonPath);
        if (!string.IsNullOrWhiteSpace(raw)) {
          string trimmed = raw.Trim();
          if (trimmed.StartsWith("[")) json = "{\"ok\":true,\"mobile_devices\":" + trimmed + ",\"count\":0}";
          else json = trimmed;
        }
      }
    } catch {}
    WriteBytes(ctx, json);
  }
  static long JsonLong(string json, string key) {
    if (string.IsNullOrEmpty(json) || string.IsNullOrEmpty(key)) return 0;
    string needle = "\"" + key + "\"";
    int i = json.IndexOf(needle, StringComparison.OrdinalIgnoreCase);
    if (i < 0) return 0;
    i = json.IndexOf(':', i + needle.Length);
    if (i < 0) return 0;
    i++;
    while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
    long n = 0;
    bool any = false;
    while (i < json.Length && char.IsDigit(json[i])) { n = n * 10 + (json[i] - '0'); i++; any = true; }
    return any ? n : 0;
  }
  static int JsonInt(string json, string key) {
    long n = JsonLong(json, key);
    if (n > int.MaxValue) return int.MaxValue;
    if (n < int.MinValue) return int.MinValue;
    return (int)n;
  }
  string FindLiveStaging(string runName, string progressJson) {
    string dest = JsonStr(progressJson ?? "", "output_path");
    if (!string.IsNullOrEmpty(dest) && dest.IndexOf("\\ib\\", StringComparison.OrdinalIgnoreCase) >= 0 && Directory.Exists(dest))
      return dest;
    string stamp = "";
    if (!string.IsNullOrEmpty(runName)) {
      Match m = Regex.Match(runName, @"(\d{8}_\d{6})$");
      if (m.Success) stamp = m.Groups[1].Value;
    }
    if (stamp.Length > 0) {
      string[] roots = new string[] { @"F:\ib", @"D:\ib", @"C:\ib", @"E:\ib" };
      for (int i = 0; i < roots.Length; i++) {
        if (!Directory.Exists(roots[i])) continue;
        try {
          string[] tails = Directory.GetDirectories(roots[i]);
          for (int t = 0; t < tails.Length; t++) {
            string cand = Path.Combine(tails[t], stamp);
            if (Directory.Exists(cand)) return cand;
          }
        } catch {}
      }
    }
    if (!string.IsNullOrEmpty(dest) && Directory.Exists(dest)) return dest;
    return "";
  }
  bool JobWasCancelled(string progressFile) {
    if (string.IsNullOrEmpty(progressFile)) return false;
    try {
      if (File.Exists(progressFile + ".cancel")) return true;
      if (!File.Exists(progressFile)) return false;
      string p = File.ReadAllText(progressFile);
      string stage = JsonStr(p, "stage");
      string item = JsonStr(p, "item");
      return stage == "cancelled" || item == "cancelled";
    } catch { return false; }
  }
  bool ResultFileReady(string resultFile) {
    if (string.IsNullOrEmpty(resultFile) || !File.Exists(resultFile)) return false;
    try {
      FileInfo fi = new FileInfo(resultFile);
      if (fi.Length < 8) return false;
      string head = "";
      using (FileStream fs = File.Open(resultFile, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)) {
        byte[] buf = new byte[Math.Min(4096, (int)fi.Length)];
        int n = fs.Read(buf, 0, buf.Length);
        head = Encoding.UTF8.GetString(buf, 0, n);
      }
      if (head.IndexOf("\"ok\"", StringComparison.OrdinalIgnoreCase) < 0) return false;
      // native_ios_job / native_mtp_job write ok:false + stage_reached acquire as a
      // start placeholder. That must not look like a finished collection.
      if (head.IndexOf("\"stage_reached\":\"acquire\"", StringComparison.OrdinalIgnoreCase) >= 0) return false;
      if (head.IndexOf("\"stage_reached\": \"acquire\"", StringComparison.OrdinalIgnoreCase) >= 0) return false;
      return true;
    } catch { return false; }
  }
  bool PidStillOurs(int pid, string metaPath) {
    if (pid <= 0) return false;
    try {
      Process p = Process.GetProcessById(pid);
      if (p == null || p.HasExited) return false;
      string n = (p.ProcessName ?? "").ToLowerInvariant();
      if (n != "powershell" && n != "pwsh" && n != "python" && n != "pythonw") return false;
      if (!string.IsNullOrEmpty(metaPath) && File.Exists(metaPath)) {
        DateTime jobTime = File.GetLastWriteTime(metaPath);
        if (p.StartTime > jobTime.AddMinutes(2)) return false;
      }
      return true;
    } catch { return false; }
  }
  bool SkipLiveScanPath(string path) {
    if (string.IsNullOrEmpty(path)) return true;
    string n = path.Replace('/', '\\');
    if (n.IndexOf("\\05_Exports", StringComparison.OrdinalIgnoreCase) >= 0) return true;
    if (n.IndexOf("\\readable_artifacts", StringComparison.OrdinalIgnoreCase) >= 0) return true;
    return false;
  }
  void KickLiveScan(string path) {
    if (SkipLiveScanPath(path) || !Directory.Exists(path)) return;
    if (Interlocked.CompareExchange(ref _liveBusy, 1, 0) != 0) return;
    string scanPath = path;
    Thread th = new Thread((ThreadStart)delegate {
      try {
        int files = 0;
        long bytes = 0;
        string newest = "";
        DateTime nt = DateTime.MinValue;
        DateTime started = DateTime.UtcNow;
        foreach (string f in Directory.EnumerateFiles(scanPath, "*", SearchOption.AllDirectories)) {
          if ((DateTime.UtcNow - started).TotalSeconds > 20) break;
          files++;
          try {
            FileInfo fi = new FileInfo(f);
            if (fi.Length > 512L * 1024 * 1024) continue;
            if ((fi.Attributes & FileAttributes.ReparsePoint) != 0) continue;
            bytes += fi.Length;
            if (fi.LastWriteTime > nt) { nt = fi.LastWriteTime; newest = f; }
          } catch {}
        }
        _liveFiles = files;
        _liveBytes = bytes;
        _liveNewest = newest ?? "";
        _livePath = scanPath;
      } catch {}
      finally { Interlocked.Exchange(ref _liveBusy, 0); }
    });
    th.IsBackground = true;
    th.Name = "aetheris-live-scan";
    th.Start();
  }
  void WriteJobList(HttpListenerContext ctx) {
    try {
      List<string> parts = new List<string>();
      if (!string.IsNullOrEmpty(JobsDir) && Directory.Exists(JobsDir)) {
        foreach (string f in Directory.GetFiles(JobsDir, "*.json")) {
          string jobId = Path.GetFileNameWithoutExtension(f);
          string meta = "";
          try { meta = File.ReadAllText(f); } catch { continue; }
          int pid = JsonInt(meta, "pid");
          string resultFile = JsonStr(meta, "result_file");
          bool resultReady = ResultFileReady(resultFile);
          bool alive = PidStillOurs(pid, f);
          if (JobWasCancelled(JsonStr(meta, "progress_file")) && !resultReady) continue;
          if (resultReady) {
            string head = "";
            try { head = File.ReadAllText(resultFile); } catch { continue; }
            bool okVal = head.IndexOf("\"ok\":false", StringComparison.OrdinalIgnoreCase) < 0
              && head.IndexOf("\"ok\": false", StringComparison.OrdinalIgnoreCase) < 0;
            string doneName = JsonStr(head, "run_name");
            string doneCase = JsonStr(meta, "case_id");
            int doneFiles = JsonInt(head, "file_count");
            if (doneFiles <= 0) doneFiles = JsonInt(head, "files_seen");
            long doneBytes = JsonLong(head, "output_size");
            if (doneBytes <= 0) doneBytes = JsonLong(head, "bytes_done");
            string doneStatus = okVal ? "completed" : "failed";
            parts.Add("{\"job_id\":\"" + JsonEscape(jobId) + "\",\"status\":\"" + doneStatus + "\",\"run_name\":\"" +
              JsonEscape(doneName) + "\",\"case_id\":\"" + JsonEscape(doneCase) +
              "\",\"detail\":\"Collection finished\",\"files_seen\":" + doneFiles +
              ",\"bytes_done\":" + doneBytes + ",\"progress_pct\":" + (okVal ? "100" : "0") + "}");
            continue;
          }
          if (!alive) continue;
          string progressFile = JsonStr(meta, "progress_file");
          string detail = "";
          string runName = "";
          int files = 0;
          long bytes = 0;
          int pct = 0;
          string progJson = "";
          string item = "";
          string outputPath = "";
          if (!string.IsNullOrEmpty(progressFile) && File.Exists(progressFile)) {
            try { progJson = File.ReadAllText(progressFile); } catch { progJson = ""; }
            detail = JsonStr(progJson, "detail");
            runName = JsonStr(progJson, "run_name");
            files = JsonInt(progJson, "files_seen");
            bytes = JsonLong(progJson, "bytes_done");
            pct = JsonInt(progJson, "progress_pct");
            item = JsonStr(progJson, "item");
            outputPath = JsonStr(progJson, "output_path");
          }
          string stage = JsonStr(progJson, "stage");
          bool sealing = stage == "seal" || item == "export_packages" ||
            (!string.IsNullOrEmpty(detail) && detail.IndexOf("package", StringComparison.OrdinalIgnoreCase) >= 0);
          bool finishedProg = stage == "complete" || stage == "failed" || stage == "error" || item == "complete";
          if (!sealing && !finishedProg) {
            string staging = FindLiveStaging(runName, progJson);
            if (!SkipLiveScanPath(staging)) {
              KickLiveScan(staging);
              if (_liveFiles > files) files = _liveFiles;
              if (_liveBytes > bytes) bytes = _liveBytes;
              if (!string.IsNullOrEmpty(_liveNewest)) item = _liveNewest;
              if (!string.IsNullOrEmpty(_livePath)) outputPath = _livePath;
            }
          } else if (sealing && pct < 95) {
            pct = 100;
          }
          string caseId = JsonStr(meta, "case_id");
          parts.Add("{\"job_id\":\"" + JsonEscape(jobId) + "\",\"status\":\"running\",\"run_name\":\"" +
            JsonEscape(runName) + "\",\"case_id\":\"" + JsonEscape(caseId) + "\",\"detail\":\"" + JsonEscape(detail) +
            "\",\"item\":\"" + JsonEscape(item) + "\",\"output_path\":\"" + JsonEscape(outputPath) +
            "\",\"files_seen\":" + files + ",\"bytes_done\":" + bytes + ",\"progress_pct\":" + pct + "}");
        }
      }
      WriteBytes(ctx, "{\"ok\":true,\"jobs\":[" + string.Join(",", parts.ToArray()) + "]}");
    } catch (Exception ex) {
      WriteBytes(ctx, "{\"ok\":false,\"jobs\":[],\"error\":\"" + JsonEscape(ex.Message) + "\"}");
    }
  }
  bool SidecarAcquireAlive() {
    if (string.IsNullOrEmpty(JobsDir) || !Directory.Exists(JobsDir)) return false;
    foreach (string f in Directory.GetFiles(JobsDir, "*.json")) {
      try {
        string meta = File.ReadAllText(f);
        int pid = JsonInt(meta, "pid");
        string resultFile = JsonStr(meta, "result_file");
        if (ResultFileReady(resultFile)) continue;
        if (JobWasCancelled(JsonStr(meta, "progress_file"))) continue;
        if (PidStillOurs(pid, f)) return true;
      } catch {}
    }
    return false;
  }
  bool AnyAcquireProcessAlive() {
    try {
      ProcessStartInfo psi = new ProcessStartInfo();
      psi.FileName = "wmic.exe";
      psi.Arguments = "process get ProcessId,Name,CommandLine /FORMAT:CSV";
      psi.RedirectStandardOutput = true;
      psi.UseShellExecute = false;
      psi.CreateNoWindow = true;
      using (Process p = Process.Start(psi)) {
        if (p == null) return false;
        string all = p.StandardOutput.ReadToEnd() ?? "";
        p.WaitForExit(4000);
        string[] needles = new string[] {
          "acq_run_", "ios_usbmux_backup", "native_ios_job", "native_mtp_job", "host-mobile-acquire"
        };
        foreach (string line in all.Split(new char[] {'\n','\r'}, StringSplitOptions.RemoveEmptyEntries)) {
          string lower = line.ToLowerInvariant();
          if (lower.IndexOf("host-drive-helper") >= 0) continue;
          for (int i = 0; i < needles.Length; i++) {
            if (lower.IndexOf(needles[i]) >= 0) return true;
          }
        }
      }
    } catch {}
    return false;
  }
  // This C# sits in an expandable PowerShell here-string: every PowerShell variable in the
  // command text below must be written `$ so it reaches powershell.exe instead of being emptied here.
  void KillMatchingAcquireProcesses() {
    try {
      ProcessStartInfo psi = new ProcessStartInfo();
      psi.FileName = "powershell.exe";
      psi.Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -Command \"Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -and `$_.CommandLine -notmatch 'host-drive-helper' -and ((`$_.Name -match 'python' -and `$_.CommandLine -match 'acq_run_|ios_usbmux_backup|native_ios_job|native_mtp_job|host-mobile-acquire') -or (`$_.Name -match 'powershell|pwsh' -and `$_.CommandLine -match 'native_ios_job|native_mtp_job|host-mobile-acquire')) } | ForEach-Object { & taskkill.exe /PID `$_.ProcessId /T /F 2>`$null }\"";
      psi.UseShellExecute = false;
      psi.CreateNoWindow = true;
      Process p = Process.Start(psi);
      if (p != null) p.WaitForExit(15000);
    } catch {}
  }
  void WriteCancelUsb(HttpListenerContext ctx) {
    string jobId = "";
    try {
      using (StreamReader reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8)) {
        string body = reader.ReadToEnd() ?? "";
        jobId = JsonStr(body, "job_id");
      }
    } catch {}
    List<string> stopped = new List<string>();
    try {
      if (!string.IsNullOrEmpty(JobsDir) && Directory.Exists(JobsDir)) {
        string[] files;
        if (!string.IsNullOrEmpty(jobId)) {
          string one = Path.Combine(JobsDir, jobId + ".json");
          files = File.Exists(one) ? new string[] { one } : new string[0];
        } else {
          files = Directory.GetFiles(JobsDir, "*.json");
        }
        for (int i = 0; i < files.Length; i++) {
          try {
            string f = files[i];
            string meta = File.ReadAllText(f);
            int pid = JsonInt(meta, "pid");
            string id = Path.GetFileNameWithoutExtension(f);
            string progressFile = JsonStr(meta, "progress_file");
            if (pid > 0) {
              try {
                ProcessStartInfo tk = new ProcessStartInfo();
                tk.FileName = "taskkill.exe";
                tk.Arguments = "/PID " + pid + " /T /F";
                tk.UseShellExecute = false;
                tk.CreateNoWindow = true;
                Process t = Process.Start(tk);
                if (t != null) t.WaitForExit(5000);
              } catch {}
              stopped.Add(id);
            }
            if (!string.IsNullOrEmpty(progressFile)) {
              try { File.WriteAllText(progressFile + ".cancel", "cancel"); } catch {}
              try {
                File.WriteAllText(progressFile,
                  "{\"stage\":\"cancelled\",\"item\":\"cancelled\",\"detail\":\"Stopped by examiner\",\"category\":\"Cancelled\",\"bytes_done\":0,\"files_seen\":0}");
              } catch {}
            }
          } catch {}
        }
      }
      if (string.IsNullOrEmpty(jobId)) {
        KillMatchingAcquireProcesses();
      } else {
        try {
          ProcessStartInfo psi = new ProcessStartInfo();
          psi.FileName = "powershell.exe";
          psi.Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -Command \"Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -and `$_.CommandLine -match [regex]::Escape('" + jobId.Replace("'", "") + "') -and `$_.CommandLine -notmatch 'host-drive-helper' } | ForEach-Object { & taskkill.exe /PID `$_.ProcessId /T /F 2>`$null }\"";
          psi.UseShellExecute = false;
          psi.CreateNoWindow = true;
          Process p = Process.Start(psi);
          if (p != null) p.WaitForExit(15000);
        } catch {}
      }
    } catch {}
    List<string> quoted = new List<string>();
    for (int i = 0; i < stopped.Count; i++) quoted.Add("\"" + JsonEscape(stopped[i]) + "\"");
    WriteJson(ctx, 200, "{\"ok\":true,\"cancelled\":[" + string.Join(",", quoted.ToArray()) + "],\"message\":\"Host collection stopped\"}");
  }
  bool ProgressSaysFinished(string progressFile) {
    if (string.IsNullOrEmpty(progressFile) || !File.Exists(progressFile)) return false;
    try {
      string p = File.ReadAllText(progressFile);
      string stage = JsonStr(p, "stage");
      string item = JsonStr(p, "item");
      string detail = JsonStr(p, "detail");
      if (stage == "complete" || stage == "failed" || stage == "error" || item == "complete") return true;
      if (!string.IsNullOrEmpty(detail) && (
          detail.IndexOf("Collection finished", StringComparison.OrdinalIgnoreCase) >= 0 ||
          detail.IndexOf("Portable packages", StringComparison.OrdinalIgnoreCase) >= 0)) return true;
      return false;
    } catch { return false; }
  }
  string SlimCompletedPayload(string jobId, string body, bool okVal, string status) {
    string runName = JsonStr(body, "run_name");
    string exports = JsonStr(body, "exports");
    string original = JsonStr(body, "original");
    string logs = JsonStr(body, "logs");
    string hashes = JsonStr(body, "hashes");
    string caseRoot = JsonStr(body, "case_root");
    string zip = JsonStr(body, "zip");
    string pas = JsonStr(body, "pas");
    string ufd = JsonStr(body, "ufd");
    string ufdx = JsonStr(body, "ufdx");
    string osFamily = JsonStr(body, "os_family");
    string err = JsonStr(body, "error");
    int doneFiles = JsonInt(body, "file_count");
    long doneSize = JsonLong(body, "output_size");
    string resultJson = "{\"ok\":" + (okVal ? "true" : "false") +
      ",\"run_name\":\"" + JsonEscape(runName) +
      "\",\"stage_reached\":\"" + (okVal ? "complete" : status) +
      "\",\"error\":\"" + JsonEscape(err) +
      "\",\"device_profile\":{\"os_family\":\"" + JsonEscape(osFamily) + "\"}" +
      ",\"paths\":{\"exports\":\"" + JsonEscape(exports) + "\",\"original\":\"" + JsonEscape(original) +
      "\",\"logs\":\"" + JsonEscape(logs) + "\",\"hashes\":\"" + JsonEscape(hashes) +
      "\",\"case_root\":\"" + JsonEscape(caseRoot) + "\",\"run_name\":\"" + JsonEscape(runName) + "\"}" +
      ",\"export_packages\":{\"packages\":{\"zip\":\"" + JsonEscape(zip) + "\",\"pas\":\"" + JsonEscape(pas) +
      "\",\"ufd\":\"" + JsonEscape(ufd) + "\",\"ufdx\":\"" + JsonEscape(ufdx) + "\"},\"full_payload_in_zip\":true}" +
      ",\"acquisition_record\":{\"output_size\":" + doneSize + ",\"file_count\":" + doneFiles + "}" +
      ",\"verification\":{\"ok\":" + (okVal ? "true" : "false") + ",\"verified\":" + doneFiles + "}" +
      ",\"evidence_package\":{\"run_name\":\"" + JsonEscape(runName) + "\",\"extraction_data\":[],\"file_count\":" +
      doneFiles + ",\"complete\":" + (okVal ? "true" : "false") + "}}";
    return "{\"ok\":true,\"status\":\"" + status + "\",\"job_id\":\"" + JsonEscape(jobId) + "\",\"result\":" + resultJson + "}";
  }
  string CompletedJobPayload(string jobId, string resultFile) {
    string body = "";
    try { body = File.ReadAllText(resultFile); } catch { body = ""; }
    bool okVal = true;
    string status = "completed";
    if (body.IndexOf("\"ok\":false", StringComparison.OrdinalIgnoreCase) >= 0 ||
        body.IndexOf("\"ok\": false", StringComparison.OrdinalIgnoreCase) >= 0) {
      okVal = false;
      status = "failed";
    }
    long len = 0;
    try { len = new FileInfo(resultFile).Length; } catch {}
    if (len > 80000 || string.IsNullOrEmpty(body) || !body.TrimStart().StartsWith("{")) {
      return SlimCompletedPayload(jobId, body, okVal, status);
    }
    return "{\"ok\":true,\"status\":\"" + status + "\",\"job_id\":\"" + JsonEscape(jobId) +
      "\",\"result\":" + body + "}";
  }
  bool WriteJobStatus(HttpListenerContext ctx, string path, string query) {
    try {
      string jobId = "";
      if (path.StartsWith("/acquisition/job/") && path.Length > "/acquisition/job/".Length)
        jobId = Uri.UnescapeDataString(path.Substring("/acquisition/job/".Length).Trim('/'));
      if (string.IsNullOrEmpty(jobId) && !string.IsNullOrEmpty(query)) {
        int i = query.IndexOf("id=", StringComparison.OrdinalIgnoreCase);
        if (i >= 0) {
          i += 3;
          int amp = query.IndexOf('&', i);
          string raw = amp < 0 ? query.Substring(i) : query.Substring(i, amp - i);
          jobId = Uri.UnescapeDataString(raw.TrimStart('?'));
        }
      }
      if (string.IsNullOrEmpty(jobId) || string.IsNullOrEmpty(JobsDir)) return false;
      string metaPath = Path.Combine(JobsDir, jobId + ".json");
      if (!File.Exists(metaPath)) return false;
      string meta = File.ReadAllText(metaPath);
      int pid = JsonInt(meta, "pid");
      string resultFile = JsonStr(meta, "result_file");
      string progressFile = JsonStr(meta, "progress_file");
      bool resultReady = ResultFileReady(resultFile);
      bool finishedProg = ProgressSaysFinished(progressFile);
      bool alive = PidStillOurs(pid, metaPath);
      if (resultReady) {
        WriteBytes(ctx, CompletedJobPayload(jobId, resultFile));
        return true;
      }
      string runName = "";
      if (alive && !finishedProg) {
        string progHead = "";
        if (!string.IsNullOrEmpty(progressFile) && File.Exists(progressFile)) {
          try { progHead = File.ReadAllText(progressFile); } catch { progHead = ""; }
        }
        runName = JsonStr(progHead, "run_name");
        int fileCount = JsonInt(progHead, "file_count");
        if (fileCount <= 0) fileCount = JsonInt(progHead, "files_seen");
        long outputSize = JsonLong(progHead, "output_size");
        if (outputSize <= 0) outputSize = JsonLong(progHead, "bytes_done");
        string detail = JsonStr(progHead, "detail");
        int pct = JsonInt(progHead, "progress_pct");
        string item = JsonStr(progHead, "item");
        string outputPath = JsonStr(progHead, "output_path");
        string casePath = JsonStr(progHead, "case_path");
        string mediaPath = JsonStr(progHead, "media_path");
        string jobStage = JsonStr(progHead, "stage");
        bool sealingJob = jobStage == "seal" || item == "export_packages" ||
          (!string.IsNullOrEmpty(detail) && detail.IndexOf("package", StringComparison.OrdinalIgnoreCase) >= 0);
        if (!sealingJob) {
          string staging = FindLiveStaging(runName, progHead);
          if (!SkipLiveScanPath(staging)) {
            KickLiveScan(staging);
            if (_liveFiles > fileCount) fileCount = _liveFiles;
            if (_liveBytes > outputSize) outputSize = _liveBytes;
            if (!string.IsNullOrEmpty(_liveNewest)) item = _liveNewest;
            if (!string.IsNullOrEmpty(_livePath)) outputPath = _livePath;
          }
        } else if (sealingJob && pct < 95) {
          pct = 100;
        }
        string jsonRun = "{\"ok\":true,\"status\":\"running\",\"job_id\":\"" + JsonEscape(jobId) +
          "\",\"detail\":\"" + JsonEscape(detail) + "\",\"progress_pct\":" + pct +
          ",\"files_seen\":" + fileCount + ",\"bytes_done\":" + outputSize +
          ",\"item\":\"" + JsonEscape(item) + "\",\"output_path\":\"" + JsonEscape(outputPath) +
          "\",\"case_path\":\"" + JsonEscape(casePath) + "\",\"media_path\":\"" + JsonEscape(mediaPath) +
          "\",\"result\":{\"ok\":false,\"run_name\":\"" + JsonEscape(runName) +
          "\",\"stage_reached\":\"acquire\",\"detail\":\"" + JsonEscape(detail) +
          "\",\"progress_pct\":" + pct + ",\"item\":\"" + JsonEscape(item) +
          "\",\"output_path\":\"" + JsonEscape(outputPath) +
          "\",\"acquisition_record\":{\"output_size\":" + outputSize + ",\"file_count\":" + fileCount + "}}}";
        WriteBytes(ctx, jsonRun);
        return true;
      }
      if (finishedProg && !resultReady) {
        string progHead = "";
        if (!string.IsNullOrEmpty(progressFile) && File.Exists(progressFile)) {
          try { progHead = File.ReadAllText(progressFile); } catch { progHead = ""; }
        }
        runName = JsonStr(progHead, "run_name");
        int fileCount = JsonInt(progHead, "file_count");
        if (fileCount <= 0) fileCount = JsonInt(progHead, "files_seen");
        long outputSize = JsonLong(progHead, "output_size");
        if (outputSize <= 0) outputSize = JsonLong(progHead, "bytes_done");
        string detail = JsonStr(progHead, "detail");
        string jobStage = JsonStr(progHead, "stage");
        bool failedProg = jobStage == "failed" || jobStage == "error";
        string doneStatus = failedProg ? "failed" : "completed";
        string jsonFin = "{\"ok\":true,\"status\":\"" + doneStatus + "\",\"job_id\":\"" + JsonEscape(jobId) +
          "\",\"detail\":\"" + JsonEscape(detail) + "\",\"progress_pct\":100,\"files_seen\":" + fileCount +
          ",\"bytes_done\":" + outputSize + ",\"item\":\"complete\",\"result\":{\"ok\":" + (failedProg ? "false" : "true") +
          ",\"run_name\":\"" + JsonEscape(runName) + "\",\"stage_reached\":\"" + (failedProg ? "failed" : "complete") +
          "\",\"detail\":\"" + JsonEscape(detail) +
          "\",\"acquisition_record\":{\"output_size\":" + outputSize + ",\"file_count\":" + fileCount +
          "},\"evidence_package\":{\"run_name\":\"" + JsonEscape(runName) +
          "\",\"extraction_data\":[],\"file_count\":" + fileCount + ",\"complete\":" + (failedProg ? "false" : "true") + "}}}";
        WriteBytes(ctx, jsonFin);
        return true;
      }
      if (JobWasCancelled(progressFile) && !resultReady) {
        string jsonCancel = "{\"ok\":true,\"status\":\"cancelled\",\"job_id\":\"" + JsonEscape(jobId) +
          "\",\"result\":{\"ok\":false,\"run_name\":\"\",\"stage_reached\":\"cancelled\",\"error\":\"Cancelled by examiner\",\"errors\":[\"Cancelled by examiner\"],\"paths\":{},\"acquisition_record\":{\"output_size\":0,\"file_count\":0}}}";
        WriteBytes(ctx, jsonCancel);
        return true;
      }
      string status = "running";
      bool okVal = true;
      string err = "";
      if (resultReady) {
        string body = "";
        try { body = File.ReadAllText(resultFile); } catch { body = ""; }
        if (string.IsNullOrEmpty(body)) {
          try {
            using (FileStream fs = File.OpenRead(resultFile)) {
              byte[] buf = new byte[Math.Min(65536, (int)Math.Max(1, fs.Length))];
              int n = fs.Read(buf, 0, buf.Length);
              body = Encoding.UTF8.GetString(buf, 0, n);
            }
          } catch { body = ""; }
        }
        if (body.IndexOf("\"ok\":false", StringComparison.OrdinalIgnoreCase) >= 0 ||
            body.IndexOf("\"ok\": false", StringComparison.OrdinalIgnoreCase) >= 0) {
          okVal = false;
          status = "failed";
        } else {
          status = "completed";
        }
        if (!string.IsNullOrEmpty(body) && body.TrimStart().StartsWith("{")) {
          string jsonDone = "{\"ok\":true,\"status\":\"" + status + "\",\"job_id\":\"" + JsonEscape(jobId) +
            "\",\"result\":" + body + "}";
          WriteBytes(ctx, jsonDone);
          return true;
        }
        runName = JsonStr(body, "run_name");
        err = JsonStr(body, "error");
        int doneFiles = JsonInt(body, "file_count");
        long doneSize = JsonLong(body, "output_size");
        string resultJson = "{\"ok\":" + (okVal ? "true" : "false") +
          ",\"run_name\":\"" + JsonEscape(runName) +
          "\",\"stage_reached\":\"" + status + "\",\"error\":\"" + JsonEscape(err) +
          "\",\"errors\":" + (okVal || string.IsNullOrEmpty(err) ? "[]" : ("[\"" + JsonEscape(err) + "\"]")) +
          ",\"method_decision\":{\"selected\":\"logical\",\"rationale\":\"Android MTP file-transfer logical copy of shared storage.\"}" +
          ",\"acquisition_record\":{\"output_size\":" + doneSize + ",\"file_count\":" + doneFiles + "}" +
          ",\"evidence_package\":{\"run_name\":\"" + JsonEscape(runName) + "\",\"extraction_data\":[],\"file_count\":" + doneFiles + ",\"complete\":" + (okVal ? "true" : "false") + "}}";
        string jsonDone2 = "{\"ok\":true,\"status\":\"" + status + "\",\"job_id\":\"" + JsonEscape(jobId) +
          "\",\"result\":" + resultJson + "}";
        WriteBytes(ctx, jsonDone2);
        return true;
      }
      status = "failed";
      okVal = false;
      err = "Acquire process exited without result";
      string errFile = JsonStr(meta, "err_file");
      if (!string.IsNullOrEmpty(errFile) && File.Exists(errFile)) {
        try {
          string tail = File.ReadAllText(errFile);
          if (!string.IsNullOrEmpty(tail)) {
            if (tail.Length > 500) tail = tail.Substring(0, 500);
            err = tail.Replace("\r", " ").Replace("\n", " ").Trim();
          }
        } catch {}
      }
      string resultJsonRun = "{\"ok\":" + (okVal ? "true" : "false") +
        ",\"run_name\":\"" + JsonEscape(runName) +
        "\",\"stage_reached\":\"" + status + "\",\"error\":\"" + JsonEscape(err) + "\"}";
      string json = "{\"ok\":true,\"status\":\"" + status + "\",\"job_id\":\"" + JsonEscape(jobId) +
        "\",\"result\":" + resultJsonRun + "}";
      WriteBytes(ctx, json);
      return true;
    } catch {
      return false;
    }
  }
  void WriteNativeUsbRun(HttpListenerContext ctx, string query) {
    try {
      string body = "";
      using (StreamReader reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8)) {
        body = reader.ReadToEnd() ?? "";
      }
      bool ios = query.IndexOf("native=ios", StringComparison.OrdinalIgnoreCase) >= 0;
      string script = ios ? NativeIosJobScript : NativeMtpJobScript;
      string via = ios ? "native_ios" : "native_mtp";
      if (string.IsNullOrEmpty(script) || !File.Exists(script)) {
        WriteBytes(ctx, "{\"ok\":false,\"error\":\"" + via + " job script missing\"}");
        return;
      }
      string stamp = Guid.NewGuid().ToString("N");
      string tmp = Path.GetTempPath();
      if (string.IsNullOrEmpty(JobsDir)) JobsDir = Path.Combine(tmp, "aetheris_acq_jobs");
      Directory.CreateDirectory(JobsDir);
      if (SidecarAcquireAlive() || AnyAcquireProcessAlive()) {
        WriteJson(ctx, 409, "{\"ok\":false,\"error\":\"Another USB collection is already running. Stop it, wait for it to finish, or register an existing backup folder for forensic analysis.\",\"busy\":true}");
        return;
      }
      string payloadFile = Path.Combine(tmp, (ios ? "acq_ios_" : "acq_mtp_") + stamp + ".json");
      string resultFile = Path.Combine(tmp, "acq_result_" + stamp + ".json");
      string progressFile = JsonStr(body, "progress_file");
      if (string.IsNullOrEmpty(progressFile))
        progressFile = Path.Combine(tmp, "acq_progress_" + stamp + ".json");
      string caseId = JsonStr(body, "case_id");
      if (body.IndexOf("\"progress_file\"", StringComparison.OrdinalIgnoreCase) < 0 && body.TrimEnd().EndsWith("}")) {
        string trimmed = body.TrimEnd();
        body = trimmed.Substring(0, trimmed.Length - 1) + ",\"progress_file\":\"" + JsonEscape(progressFile) + "\"}";
      }
      File.WriteAllText(payloadFile, body, new UTF8Encoding(false));
      try {
        File.WriteAllText(progressFile,
          "{\"stage\":\"acquire\",\"item\":\"starting\",\"detail\":\"Starting collection on this PC. Keep the phone unlocked.\",\"category\":\"Collecting\",\"bytes_done\":0,\"files_seen\":0}",
          new UTF8Encoding(false));
      } catch {}
      string outFile = Path.Combine(tmp, (ios ? "acq_ios_" : "acq_mtp_") + stamp + ".out");
      string errFile = Path.Combine(tmp, (ios ? "acq_ios_" : "acq_mtp_") + stamp + ".err");
      ProcessStartInfo psi = new ProcessStartInfo();
      psi.FileName = "powershell.exe";
      psi.Arguments = "-STA -NoProfile -ExecutionPolicy Bypass -File \"" + script +
        "\" -PayloadFile \"" + payloadFile + "\" -ResultFile \"" + resultFile + "\"";
      psi.WorkingDirectory = string.IsNullOrEmpty(RootPath) ? tmp : RootPath;
      psi.UseShellExecute = false;
      psi.CreateNoWindow = true;
      psi.RedirectStandardOutput = true;
      psi.RedirectStandardError = true;
      Process proc = Process.Start(psi);
      int pid = proc != null ? proc.Id : 0;
      if (proc != null) {
        string capturedOut = outFile;
        string capturedErr = errFile;
        proc.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) {
          if (e == null || string.IsNullOrEmpty(e.Data)) return;
          try { File.AppendAllText(capturedOut, e.Data + Environment.NewLine); } catch {}
        };
        proc.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) {
          if (e == null || string.IsNullOrEmpty(e.Data)) return;
          try { File.AppendAllText(capturedErr, e.Data + Environment.NewLine); } catch {}
        };
        try { proc.BeginOutputReadLine(); } catch {}
        try { proc.BeginErrorReadLine(); } catch {}
      }
      string meta = "{\"pid\":" + pid + ",\"result_file\":\"" + JsonEscape(resultFile) +
        "\",\"progress_file\":\"" + JsonEscape(progressFile) + "\",\"err_file\":\"" + JsonEscape(errFile) +
        "\",\"status\":\"running\",\"case_id\":\"" + JsonEscape(caseId) + "\"}";
      File.WriteAllText(Path.Combine(JobsDir, stamp + ".json"), meta, new UTF8Encoding(false));
      string json = "{\"ok\":true,\"async\":true,\"job_id\":\"" + stamp +
        "\",\"status\":\"running\",\"busy\":true,\"via\":\"" + via + "\"}";
      byte[] bytes = Encoding.UTF8.GetBytes(json);
      HttpListenerResponse res = ctx.Response;
      res.StatusCode = 202;
      res.ContentType = "application/json; charset=utf-8";
      res.AddHeader("Access-Control-Allow-Origin", "*");
      res.ContentLength64 = bytes.Length;
      res.OutputStream.Write(bytes, 0, bytes.Length);
      res.OutputStream.Close();
    } catch (Exception ex) {
      try {
        WriteBytes(ctx, "{\"ok\":false,\"error\":\"" + JsonEscape(ex.Message) + "\"}");
      } catch {}
    }
  }
  void WriteNativePreview(HttpListenerContext ctx) {
    try {
      string body = "";
      using (StreamReader reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8)) {
        body = reader.ReadToEnd() ?? "";
      }
      string adapter = JsonStr(body, "adapter");
      string deviceId = JsonStr(body, "device_id");
      string label = JsonStr(body, "label");
      if (string.IsNullOrEmpty(label)) label = JsonStr(body, "device_name");
      if (string.IsNullOrEmpty(label)) label = deviceId;
      string os = "android";
      if (adapter.IndexOf("ios", StringComparison.OrdinalIgnoreCase) >= 0) os = "ios";
      if (label.IndexOf("iPhone", StringComparison.OrdinalIgnoreCase) >= 0 ||
          label.IndexOf("iPad", StringComparison.OrdinalIgnoreCase) >= 0 ||
          label.IndexOf("Apple", StringComparison.OrdinalIgnoreCase) >= 0) os = "ios";
      else if (label.IndexOf("Android", StringComparison.OrdinalIgnoreCase) >= 0 ||
               label.IndexOf("Samsung", StringComparison.OrdinalIgnoreCase) >= 0 ||
               label.IndexOf("Galaxy", StringComparison.OrdinalIgnoreCase) >= 0) os = "android";
      bool logicalOnly = adapter.IndexOf("mtp", StringComparison.OrdinalIgnoreCase) >= 0;
      string methods = os == "ios" ? "[\"backup\",\"advanced_logical\",\"logical\"]" : "[\"logical\",\"advanced_logical\",\"backup\",\"file_system\",\"full_file_system\"]";
      string manufacturer = os == "ios" ? "Apple" : (label.IndexOf("Galaxy", StringComparison.OrdinalIgnoreCase) >= 0 || label.IndexOf("Samsung", StringComparison.OrdinalIgnoreCase) >= 0 ? "Samsung" : "");
      string mode = logicalOnly ? "mtp" : "usb";
      string lockState = logicalOnly ? "unlocked_for_mtp" : "unknown";
      string prep = os == "ios" ? "Tap Trust This Computer if prompted." : "Use File transfer / MTP if the phone asks.";
      string obs2 = logicalOnly ? "MTP collection of shared storage, recycle-bin folders and chat media. Enable USB debugging for ADB backup of app databases." : "Host USB collection via the HostDrive helper.";
      string json = "{\"ok\":true,\"device_profile\":{\"manufacturer\":\"" + JsonEscape(manufacturer) +
        "\",\"model\":\"" + JsonEscape(label) + "\",\"device_label\":\"" + JsonEscape(label) +
        "\",\"chipset\":\"\",\"serial\":\"" + JsonEscape(deviceId) + "\",\"imei\":\"\",\"udid\":\"" +
        (os == "ios" ? JsonEscape(deviceId) : "") +
        "\",\"os_family\":\"" + os + "\",\"os_version\":\"\",\"security_patch_level\":\"\",\"build_id\":\"\",\"lock_state\":\"" + lockState + "\",\"encryption_state\":\"unknown\",\"connection_mode\":\"" +
        mode + "\",\"usb_debugging_authorized\":" + (adapter == "android_adb" ? "true" : "false") +
        ",\"pairing_trusted\":" + (os == "ios" ? "true" : "false") +
        ",\"developer_mode\":false,\"rooted_or_jailbroken\":null,\"battery_percent\":null,\"network_isolated\":null,\"sim_present\":null,\"iccid\":\"\",\"imsi\":\"\",\"sd_card_present\":null,\"sd_card_identifier\":\"\",\"tool_version\":\"aetheris-acquire/host-helper\",\"license_entitlements\":[],\"required_cable\":\"USB data cable\",\"unknown_fields\":[],\"observations\":[\"Detected on the examiner Windows host over USB (no extra adapter).\",\"" +
        obs2 + "\"]},\"capability\":{\"capability_label\":\"SUPPORTED_DIRECT_LOGICAL\",\"supported_methods\":" + methods +
        ",\"blocked_methods\":{},\"warnings\":[],\"preparation_steps\":[\"Keep the phone unlocked and the data cable seated.\",\"" +
        prep + "\"]},\"preparation_steps\":[\"Keep the phone unlocked and the data cable seated.\",\"" +
        prep + "\"],\"method_profiles\":{}}";
      byte[] bytes = Encoding.UTF8.GetBytes(json);
      HttpListenerResponse res = ctx.Response;
      res.StatusCode = 200;
      res.ContentType = "application/json; charset=utf-8";
      res.AddHeader("Access-Control-Allow-Origin", "*");
      res.ContentLength64 = bytes.Length;
      res.OutputStream.Write(bytes, 0, bytes.Length);
      res.OutputStream.Close();
    } catch {
      try {
        byte[] bytes = Encoding.UTF8.GetBytes("{\"ok\":true,\"device_profile\":{\"os_family\":\"android\",\"model\":\"Android phone\",\"device_label\":\"Android phone\",\"serial\":\"\",\"connection_mode\":\"mtp\",\"required_cable\":\"USB data cable\",\"observations\":[\"Detected on the examiner Windows host over USB (no extra adapter).\"]},\"capability\":{\"capability_label\":\"SUPPORTED_DIRECT_LOGICAL\",\"supported_methods\":[\"logical\",\"advanced_logical\",\"backup\",\"file_system\",\"full_file_system\"],\"blocked_methods\":{},\"warnings\":[],\"preparation_steps\":[\"Keep the phone unlocked and the data cable seated.\"]},\"preparation_steps\":[\"Keep the phone unlocked and the data cable seated.\"],\"method_profiles\":{}}");
        HttpListenerResponse res = ctx.Response;
        res.StatusCode = 200;
        res.ContentType = "application/json; charset=utf-8";
        res.AddHeader("Access-Control-Allow-Origin", "*");
        res.ContentLength64 = bytes.Length;
        res.OutputStream.Write(bytes, 0, bytes.Length);
        res.OutputStream.Close();
      } catch {}
    }
  }
  static string QuoteArg(string value) {
    if (value == null) value = "";
    int tail = 0;
    for (int i = value.Length - 1; i >= 0 && value[i] == '\\'; i--) tail++;
    string body = tail > 0 ? value.Substring(0, value.Length - tail) : value;
    return "\"" + body + new string('\\', tail * 2) + "\"";
  }
  void WriteRefreshStatus(HttpListenerContext ctx) {
    string json = "{\"ok\":true,\"status\":\"idle\",\"busy\":false}";
    try {
      if (!string.IsNullOrEmpty(RefreshStatusFile) && File.Exists(RefreshStatusFile)) {
        string raw = File.ReadAllText(RefreshStatusFile);
        if (!string.IsNullOrWhiteSpace(raw)) json = raw;
      }
    } catch {}
    WriteBytes(ctx, json);
  }
  void StartRefreshMount(HttpListenerContext ctx) {
    try {
      string body = "";
      using (StreamReader reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8)) {
        body = reader.ReadToEnd() ?? "";
      }
      string required = JsonStr(body, "required_path");
      if (string.IsNullOrEmpty(required)) required = JsonStr(body, "path");
      required = (required ?? "").Trim().Replace('/', '\\');
      string current = "";
      try {
        if (!string.IsNullOrEmpty(RefreshStatusFile) && File.Exists(RefreshStatusFile))
          current = File.ReadAllText(RefreshStatusFile);
      } catch {}
      if (current.IndexOf("\"status\":\"running\"", StringComparison.OrdinalIgnoreCase) >= 0
          || current.IndexOf("\"status\": \"running\"", StringComparison.OrdinalIgnoreCase) >= 0) {
        WriteJson(ctx, 409, "{\"ok\":false,\"error\":\"Refresh already in progress\",\"status\":\"running\",\"busy\":true}");
        return;
      }
      if (!string.IsNullOrEmpty(required)) {
        if (required.Length < 3 || required[1] != ':') {
          WriteJson(ctx, 400, "{\"ok\":false,\"error\":\"Selected evidence path must be an absolute Windows drive path.\"}");
          return;
        }
        if (!Directory.Exists(required) && !File.Exists(required)) {
          WriteJson(ctx, 400, "{\"ok\":false,\"error\":\"Windows cannot see the selected evidence path: " + JsonEscape(required) + "\"}");
          return;
        }
      }
      string msg = string.IsNullOrEmpty(required)
        ? "Drive mount refresh started."
        : "Checking Docker access to the selected folder.";
      string statusJson = "{\"status\":\"running\",\"ok\":null,\"busy\":true,\"message\":\"" + JsonEscape(msg) +
        "\",\"required_path\":" + (string.IsNullOrEmpty(required) ? "null" : "\"" + JsonEscape(required) + "\"") + "}";
      try {
        if (!string.IsNullOrEmpty(RefreshStatusFile))
          File.WriteAllText(RefreshStatusFile, statusJson, new UTF8Encoding(false));
      } catch {}
      ProcessStartInfo psi = new ProcessStartInfo();
      psi.FileName = "powershell.exe";
      psi.UseShellExecute = false;
      psi.CreateNoWindow = true;
      string args = "-NoProfile -ExecutionPolicy Bypass -File " + QuoteArg(RefreshJobScript)
        + " -Root " + QuoteArg(RootPath)
        + " -StatusFile " + QuoteArg(RefreshStatusFile);
      if (!string.IsNullOrEmpty(required)) args += " -RequiredPath " + QuoteArg(required);
      psi.Arguments = args;
      Process.Start(psi);
      WriteJson(ctx, 202, "{\"ok\":true,\"started\":true,\"status\":\"running\",\"message\":\"" + JsonEscape(msg) + "\"}");
    } catch (Exception ex) {
      try { WriteJson(ctx, 500, "{\"ok\":false,\"error\":\"" + JsonEscape(ex.Message) + "\"}"); } catch {}
    }
  }
  public HttpListenerContext Dequeue(int timeoutMs) {
    HttpListenerContext ctx;
    if (Queue.TryTake(out ctx, timeoutMs)) return ctx;
    return null;
  }
  public void Stop() {
    _stop = true;
    try { if (_listener != null) _listener.Stop(); } catch {}
  }
}
// V1.27.0 client mobile imaging: the forensic server's copy of an image that was
// created on a client PC. The API container streams each file from the client's
// read-only share in chunks; this writer stores it under the server evidence root
// "as usual" (<root>\evidence\cases\<CASE>\...), verifies SHA-256 on disk before
// the file gets its final name, and never overwrites an existing evidence file.
// Server-to-server only: requests from a web page (Origin header) are refused.
public static class AetherisImport {
  public static string RootPath = "";
  public static string CasesRootOverride = "";
  public static string ExpectedToken = "";
  public const string PartSuffix = ".polaron-part";
  public const long MaxChunkBytes = 67108864L;
  static readonly string[] RunFolders = new string[] { "02_Original_Extraction", "03_Working_Copy", "05_Exports", "06_Reports", "07_Logs", "08_Hashes" };
  static readonly string[] CaseFolders = new string[] { "01_Authority", "02_Original_Extraction", "03_Working_Copy", "04_PA_Case", "05_Exports", "06_Reports", "07_Logs", "08_Hashes", "09_Review", "10_Disclosure" };
  static readonly Regex CaseIdRe = new Regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\\z");
  static readonly object[] Stripes = MakeStripes(64);

  const uint GENERIC_READ = 0x80000000;
  const uint GENERIC_WRITE = 0x40000000;
  const uint FILE_READ_ATTRIBUTES = 0x80;
  const uint FILE_WRITE_ATTRIBUTES = 0x100;
  const uint SHARE_READ = 1;
  const uint SHARE_WRITE = 2;
  const uint SHARE_DELETE = 4;
  const uint CREATE_ALWAYS = 2;
  const uint OPEN_EXISTING = 3;
  const uint ATTR_READONLY = 0x1;
  const uint ATTR_DIRECTORY = 0x10;
  const uint ATTR_NORMAL = 0x80;
  const uint INVALID_ATTRS = 0xFFFFFFFF;
  const uint FLAG_BACKUP_SEMANTICS = 0x02000000;
  const uint MOVEFILE_WRITE_THROUGH = 0x8;

  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr sa, uint disposition, uint flags, IntPtr template);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool CreateDirectoryW(string path, IntPtr sa);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool MoveFileExW(string from, string to, uint flags);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool DeleteFileW(string path);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern uint GetFileAttributesW(string path);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool SetFileAttributesW(string path, uint attrs);
  [DllImport("kernel32.dll", SetLastError = true)]
  static extern bool SetFileTime(SafeFileHandle h, IntPtr creation, IntPtr lastAccess, ref long lastWrite);
  [DllImport("kernel32.dll", SetLastError = true)]
  static extern bool GetFileSizeEx(SafeFileHandle h, out long size);

  static object[] MakeStripes(int n) {
    object[] s = new object[n];
    for (int i = 0; i < n; i++) s[i] = new object();
    return s;
  }
  static object LockFor(string full) {
    return Stripes[(full.ToLowerInvariant().GetHashCode() & 0x7fffffff) % Stripes.Length];
  }

  public static string CasesRoot(string caseRoot) {
    string raw = (caseRoot ?? "").Trim().Replace('\\', '/').TrimEnd('/');
    string evidenceRoot = Path.Combine(RootPath, "evidence");
    if (raw.Length == 0 || raw == "/evidence/cases") {
      string root = string.IsNullOrEmpty(CasesRootOverride) ? Path.Combine(evidenceRoot, "cases") : CasesRootOverride;
      return Path.GetFullPath(root).TrimEnd('\\');
    }
    if (raw.StartsWith("/evidence/", StringComparison.Ordinal)) {
      string current = evidenceRoot;
      foreach (string seg in raw.Substring("/evidence/".Length).Split('/')) {
        if (!SafeSegment(seg)) throw new ArgumentException("Unsafe case_root segment: " + seg);
        current = Path.Combine(current, seg);
      }
      return Path.GetFullPath(current).TrimEnd('\\');
    }
    throw new ArgumentException("Imports are written only under the forensic server evidence root (/evidence/cases).");
  }

  static bool SafeSegment(string seg) {
    if (string.IsNullOrEmpty(seg) || seg == "." || seg == ".." || seg.Length > 255) return false;
    foreach (char c in seg) {
      if (c < (char)0x20 || c == '<' || c == '>' || c == ':' || c == '"' || c == '|' || c == '?' || c == '*' || c == '\\' || c == '/') return false;
    }
    return !seg.EndsWith(PartSuffix, StringComparison.OrdinalIgnoreCase);
  }

  static string[] RelSegments(string rel) {
    string norm = (rel ?? "").Replace('\\', '/');
    if (norm.Length == 0 || norm.StartsWith("/")) throw new ArgumentException("rel must be a relative path");
    string[] segs = norm.Split('/');
    foreach (string s in segs) {
      if (!SafeSegment(s)) throw new ArgumentException("Unsafe path segment in " + rel);
    }
    if (Array.IndexOf(RunFolders, segs[0]) < 0) {
      throw new ArgumentException("rel must start with 02_Original_Extraction, 03_Working_Copy, 05_Exports, 06_Reports, 07_Logs or 08_Hashes");
    }
    if (segs.Length < 3) throw new ArgumentException("rel must name a file inside a run folder");
    return segs;
  }

  // Long-path form (\\?\) so deep phone trees beyond MAX_PATH are stored intact.
  static string Lp(string full) {
    if (full.StartsWith("\\\\?\\")) return full;
    if (full.StartsWith("\\\\")) return "\\\\?\\UNC\\" + full.Substring(2);
    return "\\\\?\\" + full;
  }

  static string CaseDir(string caseRoot, string caseId) {
    if (string.IsNullOrEmpty(caseId) || !CaseIdRe.IsMatch(caseId) || caseId.Trim('.').Length == 0) {
      throw new ArgumentException("Invalid case folder name");
    }
    return CasesRoot(caseRoot) + "\\" + caseId;
  }

  static string FullPath(string caseDir, string[] segs) {
    return caseDir + "\\" + string.Join("\\", segs);
  }

  static void EnsureParentDirs(string caseDir, string[] segs) {
    Directory.CreateDirectory(caseDir);
    string current = caseDir;
    for (int i = 0; i < segs.Length - 1; i++) {
      current = current + "\\" + segs[i];
      if (!CreateDirectoryW(Lp(current), IntPtr.Zero)) {
        int err = Marshal.GetLastWin32Error();
        uint a = GetFileAttributesW(Lp(current));
        if (err != 183 || a == INVALID_ATTRS || (a & ATTR_DIRECTORY) == 0) {
          throw new IOException("Could not create folder " + current + " (Win32 error " + err + ")");
        }
      }
    }
  }

  static long FileSize(string full) {
    uint a = GetFileAttributesW(Lp(full));
    if (a == INVALID_ATTRS) return -1;
    if ((a & ATTR_DIRECTORY) != 0) return -2;
    using (SafeFileHandle h = CreateFileW(Lp(full), FILE_READ_ATTRIBUTES, SHARE_READ | SHARE_WRITE | SHARE_DELETE, IntPtr.Zero, OPEN_EXISTING, FLAG_BACKUP_SEMANTICS, IntPtr.Zero)) {
      if (h.IsInvalid) return -1;
      long size;
      if (!GetFileSizeEx(h, out size)) return -1;
      return size;
    }
  }

  static string Hex(byte[] bytes) {
    return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant();
  }

  static string Sha256Of(string full) {
    SafeFileHandle h = CreateFileW(Lp(full), GENERIC_READ, SHARE_READ, IntPtr.Zero, OPEN_EXISTING, ATTR_NORMAL, IntPtr.Zero);
    if (h.IsInvalid) {
      int err = Marshal.GetLastWin32Error();
      h.Dispose();
      throw new IOException("Could not open " + full + " (Win32 error " + err + ")");
    }
    using (FileStream fs = new FileStream(h, FileAccess.Read, 1 << 20))
    using (SHA256 sha = SHA256.Create()) {
      return Hex(sha.ComputeHash(fs));
    }
  }

  static void SetMtime(string full, long mtimeMs) {
    using (SafeFileHandle h = CreateFileW(Lp(full), FILE_WRITE_ATTRIBUTES, SHARE_READ | SHARE_WRITE, IntPtr.Zero, OPEN_EXISTING, ATTR_NORMAL, IntPtr.Zero)) {
      if (h.IsInvalid) return;
      long ft = mtimeMs * 10000L + 116444736000000000L;
      SetFileTime(h, IntPtr.Zero, IntPtr.Zero, ref ft);
    }
  }

  static string Esc(string s) {
    if (s == null) return "";
    StringBuilder sb = new StringBuilder(s.Length + 8);
    foreach (char c in s) {
      if (c == '\\') sb.Append("\\\\");
      else if (c == '"') sb.Append("\\\"");
      else if (c < (char)0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
      else sb.Append(c);
    }
    return sb.ToString();
  }

  static string Err(string code, string message) {
    return "{\"ok\":false,\"error\":\"" + Esc(code) + "\",\"detail\":\"" + Esc(message) + "\"}";
  }

  static void Json(HttpListenerContext ctx, int status, string json) {
    try {
      byte[] bytes = Encoding.UTF8.GetBytes(json ?? "{}");
      HttpListenerResponse res = ctx.Response;
      res.StatusCode = status;
      res.ContentType = "application/json; charset=utf-8";
      res.ContentLength64 = bytes.Length;
      res.OutputStream.Write(bytes, 0, bytes.Length);
      res.OutputStream.Close();
    } catch {}
  }

  static void Drain(HttpListenerContext ctx) {
    try {
      byte[] buf = new byte[65536];
      Stream input = ctx.Request.InputStream;
      long total = 0;
      int n;
      while ((n = input.Read(buf, 0, buf.Length)) > 0) {
        total += n;
        if (total > MaxChunkBytes) break;
      }
    } catch {}
  }

  static string ReadText(HttpListenerContext ctx) {
    using (MemoryStream ms = new MemoryStream()) {
      byte[] buf = new byte[65536];
      Stream input = ctx.Request.InputStream;
      int n;
      while ((n = input.Read(buf, 0, buf.Length)) > 0) {
        ms.Write(buf, 0, n);
        if (ms.Length > MaxChunkBytes) throw new ArgumentException("Request body too large");
      }
      return Encoding.UTF8.GetString(ms.ToArray());
    }
  }

  static long HeaderLong(HttpListenerContext ctx, string name, long fallback) {
    string raw = ctx.Request.Headers[name];
    long value;
    if (string.IsNullOrEmpty(raw)) return fallback;
    if (!long.TryParse(raw.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out value)) {
      throw new ArgumentException("Header " + name + " is not a number");
    }
    return value;
  }

  static string HeaderRel(HttpListenerContext ctx) {
    string raw = (ctx.Request.Headers["X-Import-Rel"] ?? "").Trim();
    if (raw.Length == 0) throw new ArgumentException("X-Import-Rel is required");
    string b64 = raw.Replace('-', '+').Replace('_', '/');
    while (b64.Length % 4 != 0) b64 += "=";
    try { return Encoding.UTF8.GetString(Convert.FromBase64String(b64)); }
    catch (FormatException) { throw new ArgumentException("X-Import-Rel must be base64url"); }
  }

  static string[] Lines(string body) {
    List<string> outLines = new List<string>();
    foreach (string line in body.Split('\n')) {
      string l = line.TrimEnd('\r');
      if (l.Length > 0) outLines.Add(l);
    }
    return outLines.ToArray();
  }

  public static void Handle(HttpListenerContext ctx, string path) {
    try {
      if (!string.IsNullOrEmpty(ExpectedToken) && ctx.Request.Headers["X-Host-Helper-Token"] != ExpectedToken) {
        Drain(ctx);
        Json(ctx, 401, Err("unauthorized", "Missing or wrong X-Host-Helper-Token"));
        return;
      }
      if (!string.IsNullOrEmpty(ctx.Request.Headers["Origin"])) {
        Drain(ctx);
        Json(ctx, 403, Err("browser_forbidden", "Evidence import is called by the forensic server only."));
        return;
      }
      string caseDir = CaseDir(ctx.Request.Headers["X-Import-Case-Root"] ?? "", ctx.Request.Headers["X-Import-Case-Id"] ?? "");
      if (path == "/acquisition/import/prepare") Prepare(ctx, caseDir);
      else if (path == "/acquisition/import/chunk") Chunk(ctx, caseDir);
      else if (path == "/acquisition/import/commit") Commit(ctx, caseDir);
      else if (path == "/acquisition/import/seal") Seal(ctx, caseDir);
      else if (path == "/acquisition/import/abort") Abort(ctx, caseDir);
      else { Drain(ctx); Json(ctx, 404, Err("not_found", "Unknown import endpoint")); }
    } catch (ArgumentException ex) {
      Drain(ctx);
      Json(ctx, 400, Err("bad_request", ex.Message));
    } catch (Exception ex) {
      Drain(ctx);
      Json(ctx, 500, Err("import_failed", ex.Message));
    }
  }

  // Body: one "<size>TAB<rel>" line per file. Creates the case skeleton and reports
  // which files are already on the server (same size = verified by an earlier commit)
  // and which paths hold a different file (never overwritten).
  static void Prepare(HttpListenerContext ctx, string caseDir) {
    string body = ReadText(ctx);
    Directory.CreateDirectory(caseDir);
    foreach (string f in CaseFolders) Directory.CreateDirectory(Path.Combine(caseDir, f));
    StringBuilder existing = new StringBuilder();
    StringBuilder conflicts = new StringBuilder();
    int nExisting = 0;
    int nConflicts = 0;
    foreach (string line in Lines(body)) {
      int tab = line.IndexOf('\t');
      if (tab <= 0) throw new ArgumentException("prepare lines are <size>TAB<rel>");
      long size = long.Parse(line.Substring(0, tab), NumberStyles.Integer, CultureInfo.InvariantCulture);
      string rel = line.Substring(tab + 1);
      long have = FileSize(FullPath(caseDir, RelSegments(rel)));
      if (have == -1) continue;
      if (have == size) {
        if (nExisting++ > 0) existing.Append(',');
        existing.Append('"').Append(Esc(rel)).Append('"');
      } else {
        if (nConflicts++ > 0) conflicts.Append(',');
        conflicts.Append("{\"rel\":\"").Append(Esc(rel)).Append("\",\"size\":").Append(have).Append('}');
      }
    }
    Json(ctx, 200, "{\"ok\":true,\"case_dir\":\"" + Esc(caseDir) + "\",\"existing\":[" + existing.ToString() +
      "],\"conflicts\":[" + conflicts.ToString() + "]}");
  }

  static void Chunk(HttpListenerContext ctx, string caseDir) {
    string[] segs = RelSegments(HeaderRel(ctx));
    long offset = HeaderLong(ctx, "X-Import-Offset", 0);
    long declared = ctx.Request.ContentLength64;
    if (offset < 0 || declared < 0 || declared > MaxChunkBytes) {
      Drain(ctx);
      Json(ctx, 413, Err("bad_chunk", "Chunks need a Content-Length of at most 64 MiB and a non-negative offset"));
      return;
    }
    string full = FullPath(caseDir, segs);
    lock (LockFor(full)) {
      long finalSize = FileSize(full);
      if (finalSize >= 0) {
        Drain(ctx);
        Json(ctx, 409, "{\"ok\":false,\"error\":\"exists\",\"size\":" + finalSize + "}");
        return;
      }
      EnsureParentDirs(caseDir, segs);
      string part = full + PartSuffix;
      SafeFileHandle h = CreateFileW(Lp(part), GENERIC_WRITE, SHARE_READ, IntPtr.Zero, offset == 0 ? CREATE_ALWAYS : OPEN_EXISTING, ATTR_NORMAL, IntPtr.Zero);
      if (h.IsInvalid) {
        int err = Marshal.GetLastWin32Error();
        h.Dispose();
        Drain(ctx);
        if (err == 2) Json(ctx, 409, "{\"ok\":false,\"error\":\"offset_mismatch\",\"size\":0}");
        else Json(ctx, 500, Err("open_failed", "Could not open " + part + " (Win32 error " + err + ")"));
        return;
      }
      long written = 0;
      using (FileStream fs = new FileStream(h, FileAccess.Write, 1 << 20)) {
        long current = fs.Length;
        if (current != offset) {
          Drain(ctx);
          Json(ctx, 409, "{\"ok\":false,\"error\":\"offset_mismatch\",\"size\":" + current + "}");
          return;
        }
        fs.Seek(offset, SeekOrigin.Begin);
        byte[] buf = new byte[1 << 20];
        Stream input = ctx.Request.InputStream;
        int n;
        while ((n = input.Read(buf, 0, buf.Length)) > 0) {
          written += n;
          if (written > MaxChunkBytes) throw new IOException("Chunk is larger than 64 MiB");
          fs.Write(buf, 0, n);
        }
        fs.Flush();
        if (declared > 0 && written != declared) {
          fs.SetLength(offset);
          Json(ctx, 400, Err("short_body", "The chunk ended early; send it again"));
          return;
        }
      }
      Json(ctx, 200, "{\"ok\":true,\"size\":" + (offset + written) + "}");
    }
  }

  // Verifies the uploaded part on disk (size + SHA-256) and only then gives it its final
  // name, so every final file under the case folder is a verified copy.
  static void Commit(HttpListenerContext ctx, string caseDir) {
    string rel = HeaderRel(ctx);
    string[] segs = RelSegments(rel);
    long size = HeaderLong(ctx, "X-Import-Size", -1);
    long mtimeMs = HeaderLong(ctx, "X-Import-Mtime-Ms", 0);
    string expected = (ctx.Request.Headers["X-Import-Sha256"] ?? "").Trim().ToLowerInvariant();
    Drain(ctx);
    if (size < 0 || expected.Length != 64) throw new ArgumentException("commit needs X-Import-Size and X-Import-Sha256");
    string full = FullPath(caseDir, segs);
    string part = full + PartSuffix;
    lock (LockFor(full)) {
      long have = FileSize(full);
      if (have >= 0) {
        string existingSha = Sha256Of(full);
        DeleteFileW(Lp(part));
        if (have == size && existingSha == expected) {
          Json(ctx, 200, "{\"ok\":true,\"exists\":true,\"sha256\":\"" + existingSha + "\",\"path\":\"" + Esc(full) + "\"}");
        } else {
          Json(ctx, 409, "{\"ok\":false,\"error\":\"conflict\",\"detail\":\"A different file already exists at this path on the forensic server. It was not overwritten.\",\"size\":" + have + ",\"sha256\":\"" + existingSha + "\"}");
        }
        return;
      }
      long partSize = FileSize(part);
      if (partSize < 0) {
        Json(ctx, 409, Err("no_part", "Nothing was uploaded for this file"));
        return;
      }
      if (partSize != size) {
        DeleteFileW(Lp(part));
        Json(ctx, 422, "{\"ok\":false,\"error\":\"size_mismatch\",\"size\":" + partSize + "}");
        return;
      }
      string actual = Sha256Of(part);
      if (actual != expected) {
        DeleteFileW(Lp(part));
        Json(ctx, 422, "{\"ok\":false,\"error\":\"hash_mismatch\",\"sha256\":\"" + actual + "\"}");
        return;
      }
      if (mtimeMs > 0) SetMtime(part, mtimeMs);
      if (!MoveFileExW(Lp(part), Lp(full), MOVEFILE_WRITE_THROUGH)) {
        int err = Marshal.GetLastWin32Error();
        throw new IOException("Could not finalise " + full + " (Win32 error " + err + ")");
      }
      Json(ctx, 200, "{\"ok\":true,\"exists\":false,\"sha256\":\"" + actual + "\",\"path\":\"" + Esc(full) + "\"}");
    }
  }

  // Body: one rel per line. Marks the imported original extraction read-only (section 12).
  static void Seal(HttpListenerContext ctx, string caseDir) {
    int sealedCount = 0;
    int failed = 0;
    foreach (string rel in Lines(ReadText(ctx))) {
      string[] segs = RelSegments(rel);
      if (segs[0] != "02_Original_Extraction") continue;
      string lp = Lp(FullPath(caseDir, segs));
      uint a = GetFileAttributesW(lp);
      if (a == INVALID_ATTRS || (a & ATTR_DIRECTORY) != 0) { failed++; continue; }
      if (SetFileAttributesW(lp, (a & ~ATTR_NORMAL) | ATTR_READONLY)) sealedCount++;
      else failed++;
    }
    Json(ctx, 200, "{\"ok\":true,\"sealed\":" + sealedCount + ",\"failed\":" + failed + "}");
  }

  // Body: one rel per line. Removes unverified parts left by a cancelled or failed copy.
  static void Abort(HttpListenerContext ctx, string caseDir) {
    int removed = 0;
    foreach (string rel in Lines(ReadText(ctx))) {
      string full = FullPath(caseDir, RelSegments(rel));
      lock (LockFor(full)) {
        if (DeleteFileW(Lp(full + PartSuffix))) removed++;
      }
    }
    Json(ctx, 200, "{\"ok\":true,\"removed\":" + removed + "}");
  }
}
"@

[AetherisImport]::RootPath = [string]$root
[AetherisImport]::ExpectedToken = [string]$expectedToken
[AetherisImport]::CasesRootOverride = [string]$script:ImportCasesRootOverride
$dispatcher = New-Object AetherisHelperDispatcher
$dispatcher.HealthJson = (@{
    ok                  = $true
    service             = "host-drive-helper"
    port                = $Port
    bind_host           = $BindHost
    helper_version      = $script:HelperVersion
    helper_pid          = $PID
    helper_script       = $PSCommandPath
    root                = $root
    job_script          = $refreshJobScript
    job_script_version  = (Get-JobScriptVersion)
    started_utc         = $script:HelperStartedUtc
    malware_triage      = $true
    malware_remediate   = $true
    computer_name       = $script:HelperComputerName
    client_import       = $true
    import_case_root    = $script:ImportCasesRoot
} | ConvertTo-Json -Compress)
$dispatcher.AdaptersJsonPath = $script:AdaptersCacheFile
$dispatcher.DevicesJsonPath = $script:AcqDevicesCacheFile
$dispatcher.MobileJsonPath = $script:DeviceCacheFile
$dispatcher.RootPath = $root
$dispatcher.NativeMtpJobScript = Join-Path $root "scripts\native_mtp_job.ps1"
$dispatcher.NativeIosJobScript = Join-Path $root "scripts\native_ios_job.ps1"
$dispatcher.JobsDir = Join-Path $env:TEMP "aetheris_acq_jobs"
$dispatcher.ExpectedToken = [string]$expectedToken
$dispatcher.RefreshStatusFile = $script:RefreshStatusFile
$dispatcher.RefreshJobScript = $refreshJobScript
$dispatcher.Start($listener)

Write-Host "Host drive helper listening on $listenPrefix"
Write-Host "Malware Detection live triage: POST /malware/triage, GET /malware/triage/{id}, GET /malware/triage/{id}/bundle"
Write-Host "Endpoints: GET /health, GET /drives, GET /mobile-devices, GET /acquisition/adapters, GET /acquisition/devices, GET /acquisition/job, POST /acquisition/run, POST /acquisition/cancel, POST /acquisition/remove, POST /acquire, POST /refresh-drive-mounts, GET /refresh-drive-mounts, POST /resolve-folder, POST /list-dir"
Write-Host "Project root: $root"
Write-Host "Press Ctrl+C to stop."

try {
    while ($listener.IsListening) {
        $context = $dispatcher.Dequeue(500)
        if ($null -eq $context) { Test-MobileDeviceRefresh; continue }
        try {
            Handle-Request -Context $context
        }
        catch {
            try {
                Write-JsonResponse -Response $context.Response -StatusCode 500 -Body @{
                    ok    = $false
                    error = $_.Exception.Message
                }
            }
            catch {
                # Response may already be closed.
            }
        }
    }
}
finally {
    try { $dispatcher.Stop() } catch {}
    try { $listener.Close() } catch {}
}
