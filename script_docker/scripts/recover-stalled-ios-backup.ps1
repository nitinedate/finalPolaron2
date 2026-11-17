# V1.31.4: finish an iPhone collection whose backup is stuck at "iPhone backup 100%" (overall 65%).
#
# For a collection that was started BEFORE the V1.31.4 update (that collection runs the old code, which waits
# for the iPhone forever). Newer collections recover by themselves.
#
# Run on the computer that collects the iPhone (the one with the phone plugged in), in PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\recover-stalled-ios-backup.ps1
#
# What it does, in this order, and only if the backup on disk is complete:
#   1. finds the stuck backup process (ios_usbmux_backup.py) and reads its staging and case folders;
#   2. checks the backup: the iPhone's Manifest.db (written in this session, after every file) opens and every
#      file it lists is present;
#   3. links the case image folder to that backup (no copy; the backup stays where it is);
#   4. stops the stuck process. The running collection then continues by itself: shared photos and WhatsApp,
#      the evidence ZIP and the SHA-256 hashes.
# If the backup is not complete it changes nothing and says what to do.
param(
    [string]$Staging = "",          # optional: "Current write/staging location" shown on the page
    [string]$CaseImageFolder = "",  # optional: "Case image folder" shown on the page
    [switch]$CheckOnly              # report only, change nothing
)
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here "host-mobile-acquire.ps1")

function Get-StuckIosBackupProcess {
    $rows = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ([string]$_.CommandLine) -match 'ios_usbmux_backup\.py' -and ([string]$_.CommandLine) -notmatch '--verify'
    })
    foreach ($row in $rows) {
        $cmd = [string]$row.CommandLine
        $get = {
            param($name)
            $m = [regex]::Match($cmd, '--' + $name + '\s+(?:"([^"]+)"|(\S+))')
            if ($m.Success) { if ($m.Groups[1].Success) { return $m.Groups[1].Value } else { return $m.Groups[2].Value } }
            return ""
        }
        [pscustomobject]@{
            ProcessId = [int]$row.ProcessId
            Started   = $row.CreationDate
            Udid      = [string](& $get 'udid')
            Dest      = [string](& $get 'dest')
            Stamp     = [string](& $get 'stamp')
        }
    }
}

function Find-IosStaging {
    param([string]$Udid, [string]$Stamp)
    $tail = ($Udid -replace '[^A-Za-z0-9]', '')
    if ($tail.Length -gt 8) { $tail = $tail.Substring($tail.Length - 8) }
    foreach ($letter in @("F", "D", "C", "E")) {
        $cand = Join-Path (Join-Path (Join-Path ($letter + ":\") "ib") $tail) $Stamp
        if (Test-Path -LiteralPath (Join-Path $cand $Udid)) { return $cand }
    }
    return ""
}

function New-CaseLinkToBackup {
    param([string]$Link, [string]$Target)
    $out = cmd.exe /c "mklink /J `"$Link`" `"$Target`"" 2>&1 | Out-String
    return $out.Trim()
}

function Stop-StuckBackup {
    param([int]$ProcessId)
    & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
}

function Invoke-RecoverStalledIosBackup {
    $procs = @(Get-StuckIosBackupProcess)
    $proc = $null
    if ($procs.Count -gt 1) {
        Write-Host ("More than one iPhone backup is running (PIDs {0}); using the newest." -f (($procs | ForEach-Object { $_.ProcessId }) -join ", ")) -ForegroundColor Yellow
        $proc = $procs | Sort-Object Started -Descending | Select-Object -First 1
    } elseif ($procs.Count -eq 1) {
        $proc = $procs[0]
    }
    if (-not $proc -and -not ($Staging -and $CaseImageFolder)) {
        Write-Host "No running iPhone backup (ios_usbmux_backup.py) was found on this computer." -ForegroundColor Yellow
        Write-Host "If the page still shows the collection in progress, run this on the computer the iPhone is plugged into."
        return 1
    }
    $udid = $(if ($proc) { $proc.Udid } else { "" })
    if (-not $CaseImageFolder -and $proc) { $CaseImageFolder = $proc.Dest }
    if (-not $Staging -and $proc) { $Staging = Find-IosStaging -Udid $udid -Stamp $proc.Stamp }
    if (-not $udid -and $Staging) {
        $dir = Get-ChildItem -LiteralPath $Staging -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($dir) { $udid = $dir.Name }
    }
    if (-not $Staging -or -not $udid -or -not $CaseImageFolder) {
        Write-Host "Could not work out the staging or case folder. Run again with -Staging and -CaseImageFolder as shown on the page." -ForegroundColor Red
        return 1
    }
    $backup = Join-Path $Staging $udid
    $since = $(if ($proc -and $proc.Started) { [datetime]$proc.Started } else { [datetime]::MinValue })
    Write-Host ("Backup on disk : {0}" -f $backup)
    Write-Host ("Case image     : {0}" -f $CaseImageFolder)
    $check = Invoke-IosBackupVerify -BackupDir $backup -Since $since
    if (-not $check.complete) {
        Write-Host ("The iPhone has NOT finished sending the backup ({0}; {1} of {2} listed files missing)." -f $check.reason, $check.missing, $check.files_in_manifest) -ForegroundColor Yellow
        Write-Host "Unlock the iPhone, keep the screen on (Settings > Display & Brightness > Auto-Lock > Never while collecting), wait 5 minutes and run this again."
        Write-Host "If it stays like this, choose Cancel collection and start the collection again: with V1.31.4 it resumes by itself."
        return 2
    }
    Write-Host ("The backup is complete: Manifest.db lists {0} files, all present." -f $check.files_in_manifest) -ForegroundColor Green
    if ($CheckOnly) {
        Write-Host "-CheckOnly: nothing changed."
        return 0
    }
    New-Item -ItemType Directory -Force -Path $CaseImageFolder | Out-Null
    $link = Join-Path $CaseImageFolder $udid
    if (-not (Test-IosBackupComplete -Path $link)) {
        if (Test-Path -LiteralPath $link) {
            Write-Host ("{0} exists but is not a complete backup; it is left alone. Remove it and run again." -f $link) -ForegroundColor Red
            return 1
        }
        $mk = New-CaseLinkToBackup -Link $link -Target $backup
        if (-not (Test-IosBackupComplete -Path $link)) {
            Write-Host ("Could not link the case folder to the backup: {0}" -f $mk) -ForegroundColor Red
            return 1
        }
        (@{
            case_path    = $link
            payload_path = $backup
            type         = "ios_backup"
            complete     = $true
            note         = "Linked by recover-stalled-ios-backup.ps1 after the iPhone did not end the backup session."
        } | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath (Join-Path $CaseImageFolder "EXTERNAL_IOS_PAYLOAD.json") -Encoding UTF8
        Write-Host ("Linked {0} -> {1}" -f $link, $backup)
    }
    if ($proc) {
        Stop-StuckBackup -ProcessId $proc.ProcessId
        Write-Host ("Stopped the stuck backup process (PID {0})." -f $proc.ProcessId)
    }
    Write-Host "Done. The collection on the page now continues by itself: shared photos and WhatsApp (keep the iPhone connected and unlocked), then the evidence ZIP and SHA-256 hashes." -ForegroundColor Green
    Write-Host ("Keep {0}: the case image folder links to it." -f $Staging)
    return 0
}

if ($env:POLARON_RECOVER_FUNCTIONS_ONLY -ne "1") {
    exit (Invoke-RecoverStalledIosBackup)
}
