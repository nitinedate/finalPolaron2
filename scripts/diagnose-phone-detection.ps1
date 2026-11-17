# V1.31.7: why does the Mobile page not show the phone that is plugged into this computer?
#
# Run on the computer the phone is plugged into, in PowerShell (no administrator needed):
#   powershell -ExecutionPolicy Bypass -File scripts\diagnose-phone-detection.ps1
#
# Read-only: it changes nothing, stops nothing and does not touch the phone. It writes a report to
# %TEMP%\polaron-phone-detection.txt and prints its path. The report lists USB device IDs and phone
# serial numbers; it contains no passwords or tokens.
#
# It collects:
#   1. the Polaron helper this page talks to (127.0.0.1:9876): version, folder, and the phone list it serves;
#   2. what Windows shows on USB (pnputil), and for each Android phone what the helper makes of it
#      (File transfer, USB debugging, charging only, no driver);
#   3. adb devices -l, when adb is installed;
#   4. the Windows portable-device (MTP) entries;
#   5. phone collection processes still running on this computer.
param(
    [int]$Port = $(if ($env:HOST_DRIVE_HELPER_PORT) { [int]$env:HOST_DRIVE_HELPER_PORT } else { 9876 }),
    [string]$OutFile = "",
    [string]$PnputilOutput = ""   # optional: a saved "pnputil /enum-devices /connected /ids" output to read instead
)
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$helperFile = Join-Path $here "host-drive-helper.ps1"
$tempDir = $(if ($env:TEMP) { $env:TEMP } else { [IO.Path]::GetTempPath() })
$sysRoot = $(if ($env:SystemRoot) { $env:SystemRoot } else { "C:\Windows" })
$localApp = $(if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $tempDir })
if (-not $OutFile) { $OutFile = Join-Path $tempDir "polaron-phone-detection.txt" }
$RequiredHelperVersion = "5.62"

$report = New-Object System.Collections.Generic.List[string]
$summary = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$Text) { $report.Add($Text) }
function Add-Section([string]$Title) { $report.Add(""); $report.Add("=== " + $Title + " ===") }
function Add-Finding([string]$Text) { $summary.Add("- " + $Text) }

function Compare-Version([string]$A, [string]$B) {
    $pa = @(([string]$A).Split('.') | ForEach-Object { $n = 0; [void][int]::TryParse($_, [ref]$n); $n })
    $pb = @(([string]$B).Split('.') | ForEach-Object { $n = 0; [void][int]::TryParse($_, [ref]$n); $n })
    $len = [Math]::Max($pa.Count, $pb.Count)
    for ($i = 0; $i -lt $len; $i++) {
        $x = $(if ($i -lt $pa.Count) { $pa[$i] } else { 0 })
        $y = $(if ($i -lt $pb.Count) { $pb[$i] } else { 0 })
        if ($x -lt $y) { return -1 }
        if ($x -gt $y) { return 1 }
    }
    return 0
}

function Invoke-Helper([string]$Path) {
    $headers = @{ Accept = "application/json" }
    if ($env:HOST_DRIVE_HELPER_TOKEN) { $headers["X-Host-Helper-Token"] = $env:HOST_DRIVE_HELPER_TOKEN }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:" + $Port + $Path) -Headers $headers -TimeoutSec 8
        return @{ ok = $true; text = [string]$r.Content }
    } catch {
        return @{ ok = $false; text = [string]$_.Exception.Message }
    }
}

Add-Line ("Polaron phone detection report - " + (Get-Date).ToString("yyyy-MM-dd HH:mm:ss zzz"))
Add-Line ("Computer: " + $env:COMPUTERNAME + "   Windows: " + [Environment]::OSVersion.VersionString + "   PowerShell: " + $PSVersionTable.PSVersion)
Add-Line ("This script's folder: " + $root)

# --- 1. helper -------------------------------------------------------------------------------------
Add-Section "1. Polaron helper on this computer (http://127.0.0.1:$Port)"
$diskVersion = ""
try {
    $m = Select-String -LiteralPath $helperFile -Pattern '\$script:HelperVersion\s*=\s*"([0-9.]+)"' -ErrorAction Stop | Select-Object -First 1
    if ($m) { $diskVersion = $m.Matches[0].Groups[1].Value }
} catch {}
Add-Line ("Helper version in this folder: " + $(if ($diskVersion) { $diskVersion } else { "(not found)" }))
$health = Invoke-Helper "/health"
$runningVersion = ""
$helperRoot = ""
if ($health.ok) {
    Add-Line ("/health: " + $health.text)
    try {
        $h = $health.text | ConvertFrom-Json
        $runningVersion = [string]$h.helper_version
        $helperRoot = [string]$h.root
    } catch {}
    if (-not $runningVersion -or (Compare-Version $runningVersion $RequiredHelperVersion) -lt 0) {
        Add-Finding ("The helper running on this computer is " + $(if ($runningVersion) { "version " + $runningVersion } else { "an old version" }) + ". This release needs " + $RequiredHelperVersion + " or later to see Android phones reliably. On the Mobile acquisition page click Update this computer and open the downloaded file, or close any phone collection and run: powershell -ExecutionPolicy Bypass -File scripts\ensure-host-drive-helper.ps1 -ForceRestart")
    }
    if ($helperRoot) {
        $a = ""; $b = ""
        try { $a = [IO.Path]::GetFullPath($helperRoot).TrimEnd('\'); $b = [IO.Path]::GetFullPath($root).TrimEnd('\') } catch {}
        if ($a -and $b -and -not $a.Equals($b, [StringComparison]::OrdinalIgnoreCase)) {
            Add-Finding ("The running helper starts from " + $helperRoot + ", not from this folder (" + $root + "). Update that folder with this release, or run scripts\ensure-host-drive-helper.ps1 -ForceRestart from this folder.")
        }
    }
} else {
    Add-Line ("/health failed: " + $health.text)
    Add-Finding "The Polaron helper is not answering on this computer, so the page cannot see any phone here. Run: powershell -ExecutionPolicy Bypass -File scripts\ensure-host-drive-helper.ps1"
}
$helperList = ""
if ($health.ok) {
    $devs = Invoke-Helper "/acquisition/devices"
    $helperList = [string]$devs.text
    Add-Line ("/acquisition/devices: " + $devs.text)
    $mob = Invoke-Helper "/mobile-devices"
    Add-Line ("/mobile-devices: " + $mob.text)
}
try {
    $procs = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object { ([string]$_.CommandLine) -match 'host-drive-helper\.ps1' })
    foreach ($p in $procs) { Add-Line ("Helper process " + $p.ProcessId + ": " + [string]$p.CommandLine) }
} catch {}

# --- 2. USB devices (pnputil) ------------------------------------------------------------------------
Add-Section "2. USB devices Windows shows (pnputil /enum-devices /connected /ids)"
$lines = @()
$pnputil = $sysRoot.TrimEnd('\') + "\System32\pnputil.exe"
$tmp = Join-Path $tempDir ("polaron_diag_pnputil_" + [guid]::NewGuid().ToString("N") + ".txt")
if ($PnputilOutput) {
    $lines = @(Get-Content -LiteralPath $PnputilOutput -ErrorAction SilentlyContinue)
    Add-Line ("Read from " + $PnputilOutput)
} elseif (Test-Path -LiteralPath $pnputil) {
    try {
        $proc = Start-Process -FilePath $pnputil -ArgumentList @("/enum-devices", "/connected", "/ids") -PassThru -WindowStyle Hidden -RedirectStandardOutput $tmp -RedirectStandardError ($tmp + ".err")
        # Never stop pnputil: aborting the enumeration can reset the phone's USB connection.
        $done = $false
        try { $done = $proc.WaitForExit(45000) } catch { $done = $false }
        if ($done -and (Test-Path -LiteralPath $tmp)) {
            $lines = @(Get-Content -LiteralPath $tmp -ErrorAction SilentlyContinue)
            Remove-Item -LiteralPath $tmp, ($tmp + ".err") -Force -ErrorAction SilentlyContinue
        } else {
            Add-Line "pnputil did not finish within 45 seconds (it is left to finish by itself)."
            Add-Finding "Windows took too long to list USB devices (pnputil). Unplug and replug the phone, wait 10 seconds, and run this report again."
        }
    } catch { Add-Line ("pnputil failed: " + $_.Exception.Message) }
} else {
    Add-Line "pnputil.exe not found."
}
# Show every USB block except hubs, root hubs and host controllers.
$block = New-Object System.Collections.Generic.List[string]
$blocks = New-Object System.Collections.Generic.List[object]
foreach ($line in $lines) {
    if ([string]$line -match '^\s*Instance ID:') {
        if ($block.Count) { $blocks.Add($block.ToArray()) }
        $block = New-Object System.Collections.Generic.List[string]
    }
    if (([string]$line).Trim()) { $block.Add([string]$line) }
}
if ($block.Count) { $blocks.Add($block.ToArray()) }
$shown = 0
foreach ($b in $blocks) {
    $first = [string]$b[0]
    if ($first -notmatch '(?i)Instance ID:\s+USB\\VID_') { continue }
    $text = ($b -join "`n")
    if ($text -match '(?i)Class_09|Root Hub|USB Hub|Host Controller') { continue }
    foreach ($l in $b) { Add-Line $l }
    Add-Line ""
    $shown++
}
if (-not $shown -and $lines.Count) { Add-Line "No USB devices with a vendor ID are connected." }

# What the helper makes of the Android phones on USB.
Add-Section "2b. Android phones on USB, as the helper classifies them"
$classifier = $null
try {
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($helperFile, [ref]$null, [ref]$null)
    $classifier = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-AndroidUsbStates' }, $true)
} catch {}
$states = $null
if ($classifier -and $lines.Count) {
    try {
        . ([scriptblock]::Create($classifier.Extent.Text))
        $states = Get-AndroidUsbStates -Lines $lines
    } catch { Add-Line ("Could not classify: " + $_.Exception.Message) }
} elseif (-not $classifier) {
    Add-Line "This folder's host-drive-helper.ps1 is older than V1.31.7 (no Android USB classifier)."
}
$anyAndroid = $false
if ($states) {
    foreach ($k in @($states.MtpKeys)) {
        $anyAndroid = $true
        Add-Line ("File transfer (MTP) is on for VID|PID " + $k + ": the phone can be collected.")
    }
    foreach ($r in @($states.Rows)) {
        $anyAndroid = $true
        Add-Line ("Not usable yet: " + $r.name + " [" + $r.instance_id + "] state=" + $r.state)
        Add-Finding ($r.name + " is plugged in but " + $(if ($r.state -eq 'driver_missing') { "Windows has no driver for its current USB mode" } else { "only for charging" }) + ". " + $r.guidance)
    }
}
$adbIfaces = @($blocks | Where-Object { ($_ -join ' ') -match '(?i)SubClass_42&Prot_01|MS_COMP_ADB' })
foreach ($b in $adbIfaces) {
    $anyAndroid = $true
    $status = (@($b | Where-Object { $_ -match '(?i)^\s*Status:' }) -join ' ').Trim()
    Add-Line ("USB debugging interface: " + ([string]$b[0]).Trim() + "  " + $status)
}
if (-not $anyAndroid -and $lines.Count) {
    Add-Line "No Android phone is visible on USB."
    Add-Finding "Windows does not see an Android phone on USB at all. Use a data cable (many cables only charge), plug straight into the computer (not a hub), unlock the phone, and on the phone choose USB: File transfer. Then run this report again."
}

# --- 3. adb ------------------------------------------------------------------------------------------
Add-Section "3. adb devices -l"
$adb = $null
foreach ($p in @(
    (Join-Path $root "tools\platform-tools\adb.exe"),
    (Join-Path $localApp "Android\Sdk\platform-tools\adb.exe"),
    (Join-Path $localApp "Microsoft\WinGet\Links\adb.exe"),
    "C:\Android\platform-tools\adb.exe",
    "C:\platform-tools\adb.exe")) {
    if ($p -and (Test-Path -LiteralPath $p)) { $adb = $p; break }
}
if (-not $adb) { $c = Get-Command "adb.exe" -ErrorAction SilentlyContinue; if ($c) { $adb = $c.Source } }
if ($adb) {
    Add-Line ("adb: " + $adb)
    $atmp = Join-Path $tempDir ("polaron_diag_adb_" + [guid]::NewGuid().ToString("N") + ".txt")
    try {
        $ap = Start-Process -FilePath $adb -ArgumentList @("devices", "-l") -PassThru -WindowStyle Hidden -RedirectStandardOutput $atmp -RedirectStandardError ($atmp + ".err")
        $ok = $false
        try { $ok = $ap.WaitForExit(20000) } catch { $ok = $false }
        if ($ok) {
            $out = @(Get-Content -LiteralPath $atmp -ErrorAction SilentlyContinue) + @(Get-Content -LiteralPath ($atmp + ".err") -ErrorAction SilentlyContinue)
            foreach ($l in $out) { Add-Line $l }
            if (($out -join "`n") -match '\bunauthorized\b') { Add-Finding "adb sees the phone as unauthorized: unlock the phone and tap Allow on the USB debugging prompt (tick Always allow), then Rescan." }
            Remove-Item -LiteralPath $atmp, ($atmp + ".err") -Force -ErrorAction SilentlyContinue
        } else {
            Add-Line "adb did not answer within 20 seconds."
        }
    } catch { Add-Line ("adb failed: " + $_.Exception.Message) }
} else {
    Add-Line "adb is not installed (not needed for File transfer collections)."
}

# --- 4. WPD ------------------------------------------------------------------------------------------
Add-Section "4. Windows portable devices (MTP) registry"
foreach ($reg in @("HKLM:\SOFTWARE\Microsoft\Windows Portable Devices\Devices", "HKCU:\SOFTWARE\Microsoft\Windows Portable Devices\Devices")) {
    if (-not (Test-Path -LiteralPath $reg)) { Add-Line ($reg + ": not present"); continue }
    foreach ($k in @(Get-ChildItem -LiteralPath $reg -ErrorAction SilentlyContinue)) {
        $props = Get-ItemProperty -LiteralPath $k.PSPath -ErrorAction SilentlyContinue
        if ([string]$k.PSChildName -notmatch '(?i)usb') { continue }
        Add-Line ([string]$props.FriendlyName + "  <-  " + [string]$k.PSChildName)
    }
}

# --- 5. processes --------------------------------------------------------------------------------------
Add-Section "5. Phone collection processes running on this computer"
$found = 0
try {
    foreach ($p in @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue)) {
        $cmd = [string]$p.CommandLine
        if ($cmd -match '(?i)ios_usbmux_backup|ios_usbmux_afc|host-mobile-acquire|native_ios_job|native_mtp_job|mtp_logical_copy|mtp_copyhere_once|\bpnputil\b' -or [string]$p.Name -match '(?i)^adb\.exe$') {
            Add-Line ([string]$p.ProcessId + " (parent " + [string]$p.ParentProcessId + "): " + $cmd)
            $found++
        }
    }
} catch {}
if (-not $found) { Add-Line "None." }

# --- write -------------------------------------------------------------------------------------------
if (-not $summary.Count) {
    if ($helperList -match '"os_family"\s*:\s*"android"') {
        $summary.Add("- The helper lists an Android phone. Click Rescan on the Android acquisition page.")
    } else {
        $summary.Add("- Nothing obvious found. Send this report to support.")
    }
}
$all = New-Object System.Collections.Generic.List[string]
$all.Add("WHAT TO DO")
foreach ($s in $summary) { $all.Add($s) }
$all.Add("")
foreach ($l in $report) { $all.Add($l) }
[IO.File]::WriteAllLines($OutFile, $all.ToArray(), (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "WHAT TO DO"
foreach ($s in $summary) { Write-Host $s }
Write-Host ""
Write-Host ("Full report: " + $OutFile)
