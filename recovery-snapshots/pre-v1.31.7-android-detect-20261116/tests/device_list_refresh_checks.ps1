# V1.31.6 offline checks: the workstation helper's phone list follows the cables.
# The helper's background refresh job is loaded from host-drive-helper.ps1 and run one pass at a time
# against a simulated Windows registry and process list.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }

$helper = Join-Path $ProjectRoot 'scripts/host-drive-helper.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($helper, [ref]$null, [ref]$null)
$job = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Start-Job' -and $n.Extent.Text -match 'aetheris-mobile-refresh' }, $true)
Assert-True ($null -ne $job) 'Found the device refresh job in host-drive-helper.ps1'
$sb = $job.Find({ param($n) $n -is [System.Management.Automation.Language.ScriptBlockExpressionAst] }, $true).ScriptBlock.GetScriptBlock()

$tmp = Join-Path ([IO.Path]::GetTempPath()) ('polaron-devlist-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$cacheFile = Join-Path $tmp 'mobile.json'
$acqFile = Join-Path $tmp 'acq.json'
$env:SystemRoot = $tmp   # no pnputil.exe here: the helper uses its registry fallback
$env:TEMP = $tmp
$env:AETHERIS_DEVICE_REFRESH_FUNCTIONS_ONLY = '1'
. $sb $cacheFile $acqFile
$env:AETHERIS_DEVICE_REFRESH_FUNCTIONS_ONLY = ''
function Set-UsbStayAwake { param([bool]$On) }

# --- simulated Windows ------------------------------------------------------------------------
$adbIface = 'USB\VID_04E8&PID_6860&MI_01\7&2B1D0B45&0&0001'
$samsungMtp = 'USB\VID_04E8&PID_6860\R58N12ABCDE'
$iphone = 'USB\VID_05AC&PID_12A8\00008150000265C0CBB401C'
$script:running = @{}      # service -> instance ids Windows is running
$script:enumProps = @{
    $adbIface   = @{ DeviceDesc = '@oem12.inf,%adb%;SAMSUNG Android ADB Interface'; CompatibleIDs = @('USB\Class_ff&SubClass_42&Prot_01', 'USB\Class_ff&SubClass_42', 'USB\Class_ff') }
    $samsungMtp = @{ DeviceDesc = '@wpdmtp.inf,%mtp%;Galaxy S23'; CompatibleIDs = @('USB\Class_06&SubClass_01&Prot_01') }
    $iphone     = @{ DeviceDesc = '@usbaapl64.inf,%apple%;Apple Mobile Device USB Composite Device'; CompatibleIDs = @('USB\Class_ff') }
}
$script:wpd = @()          # Windows Portable Devices registry entries
$script:procs = @()
function Join-Path { param([string]$Path, [string]$ChildPath) if ($Path -like 'svc::*' -or $Path -like 'HKLM:*') { return "$Path\$ChildPath" } Microsoft.PowerShell.Management\Join-Path -Path $Path -ChildPath $ChildPath }
function Test-Path { param([string]$LiteralPath, $PathType)
    if ($LiteralPath -like 'svc::*\Enum') { return $script:running.ContainsKey(($LiteralPath -replace '^svc::', '' -replace '\\Enum$', '')) }
    if ($LiteralPath -eq 'HKLM:\SOFTWARE\Microsoft\Windows Portable Devices\Devices') { return $true }
    if ($LiteralPath -like 'HKCU:*') { return $false }
    if ($LiteralPath -like '*pnputil.exe') { return $false }
    Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath }
function Get-ChildItem { param([string]$LiteralPath, $ErrorAction)
    if ($LiteralPath -eq 'HKLM:\SYSTEM\CurrentControlSet\Services') { return @(@('WinUSB', 'WUDFRd', 'usbccgp', 'USBAAPL64', 'disk') | ForEach-Object { [pscustomobject]@{ PSPath = "svc::$_"; PSChildName = $_ } }) }
    if ($LiteralPath -eq 'HKLM:\SOFTWARE\Microsoft\Windows Portable Devices\Devices') { return @($script:wpd | ForEach-Object { [pscustomobject]@{ PSPath = "wpd::$($_.Id)"; PSChildName = $_.Id } }) }
    return @() }
function Get-ItemProperty { param([string]$LiteralPath, $ErrorAction)
    if ($LiteralPath -like 'svc::*\Enum') {
        $ids = @($script:running[($LiteralPath -replace '^svc::', '' -replace '\\Enum$', '')])
        $o = [ordered]@{ Count = $ids.Count }; for ($i = 0; $i -lt $ids.Count; $i++) { $o["$i"] = $ids[$i] }; return [pscustomobject]$o }
    if ($LiteralPath -like 'HKLM:\SYSTEM\CurrentControlSet\Enum\*') {
        $id = $LiteralPath.Substring('HKLM:\SYSTEM\CurrentControlSet\Enum\'.Length)
        if ($script:enumProps.ContainsKey($id)) { return [pscustomobject]$script:enumProps[$id] }; return $null }
    if ($LiteralPath -like 'wpd::*') { $id = $LiteralPath.Substring(5); $e = $script:wpd | Where-Object { $_.Id -eq $id } | Select-Object -First 1; return [pscustomobject]@{ FriendlyName = $e.Name } }
    return $null }
function Get-CimInstance { param($ClassName, $Filter, $ErrorAction) return $script:procs }
function Read-Acq { return (Get-Content -LiteralPath $acqFile -Raw | ConvertFrom-Json) }
function Set-Plugged([string[]]$What) {
    $script:running = @{}; $script:wpd = @()
    if ($What -contains 'android-adb') { $script:running['WinUSB'] = @($adbIface); $script:running['usbccgp'] = @($samsungMtp) }
    if ($What -contains 'android-mtp') { $script:running['WUDFRd'] = @($samsungMtp); $script:wpd += @{ Id = '\\?\usb#vid_04e8&pid_6860#r58n12abcde#{6ac27878-a6fa-4155-ba85-f98f491d4f33}'; Name = 'Galaxy S23' } }
    if ($What -contains 'iphone') { $script:running['USBAAPL64'] = @($iphone); $script:wpd += @{ Id = '\\?\usb#vid_05ac&pid_12a8#00008150000265c0cbb401c#{6ac27878-a6fa-4155-ba85-f98f491d4f33}'; Name = 'Apple iPhone' } }
}
$helperPid = 4000
$leftoverBackup = [pscustomobject]@{ ProcessId = 5100; ParentProcessId = 4999; Name = 'python.exe'; CommandLine = 'python.exe C:\Polaron\scripts\ios_usbmux_backup.py --udid 00008150-0002659C0CBB401C --stamp 20261116_180455' }
$helperProc = [pscustomobject]@{ ProcessId = $helperPid; ParentProcessId = 1; Name = 'powershell.exe'; CommandLine = 'powershell.exe -File C:\Polaron\scripts\host-drive-helper.ps1' }

try {
    # 1. iPhone unplugged, Android (USB debugging) plugged in, a leftover backup from an earlier run still alive.
    Set-Plugged @('android-adb')
    $script:procs = @($helperProc, $leftoverBackup)
    $source = Update-DeviceCacheOnce
    $acq = Read-Acq
    Assert-True (@(Get-UsbTransferProcesses).Count -eq 0) 'A leftover backup whose collection is gone is not a copy in progress'
    Assert-True ($source -eq 'pnputil' -and -not $acq.note) 'The list is refreshed normally'
    Assert-True (@($acq.devices | Where-Object { $_.os_family -eq 'android' -and $_.adapter -eq 'android_adb' }).Count -eq 1) 'The Android phone with USB debugging is listed'
    Assert-True (@($acq.devices | Where-Object { $_.os_family -eq 'ios' }).Count -eq 0) 'The unplugged iPhone is not listed'
    Assert-True ((Get-Content -LiteralPath $acqFile -Raw) -match '"scanned_utc":"\d{4}-\d\d-\d\dT') 'The list says when it was read'

    # 2. Android in File transfer (MTP) mode.
    Set-Plugged @('android-mtp')
    $null = Update-DeviceCacheOnce
    $acq = Read-Acq
    Assert-True (@($acq.devices | Where-Object { $_.adapter -eq 'android_mtp' -and $_.label -eq 'Galaxy S23' }).Count -eq 1) 'An Android phone in File transfer mode is listed'

    # 3. A real iPhone copy is running (parent job alive) and the examiner plugs in the Android phone.
    Set-Plugged @('iphone', 'android-adb')
    $iosJob = [pscustomobject]@{ ProcessId = 4500; ParentProcessId = $helperPid; Name = 'powershell.exe'; CommandLine = 'powershell.exe -File C:\Polaron\scripts\native_ios_job.ps1 -PayloadFile x' }
    $activeBackup = [pscustomobject]@{ ProcessId = 4600; ParentProcessId = 4500; Name = 'python.exe'; CommandLine = 'python.exe C:\Polaron\scripts\ios_usbmux_backup.py --udid X --stamp S' }
    $script:procs = @($helperProc, $iosJob, $activeBackup)
    $source = Update-DeviceCacheOnce
    $acq = Read-Acq
    Assert-True ($source -eq 'registry' -and $acq.source -eq 'registry') 'During a copy the list is read from the registry (pnputil is not run)'
    Assert-True ([string]$acq.note -match 'A phone copy is running on this computer \(iPhone collection, iPhone backup\)') 'The list says why'
    Assert-True (@($acq.devices | Where-Object { $_.adapter -eq 'android_adb' }).Count -eq 1 -and @($acq.devices | Where-Object { $_.os_family -eq 'ios' }).Count -eq 1) 'Both phones are listed during the copy'

    # 4. The iPhone is unplugged while that collection is still running: it disappears.
    Set-Plugged @('android-adb')
    $null = Update-DeviceCacheOnce
    Assert-True (@((Read-Acq).devices | Where-Object { $_.os_family -eq 'ios' }).Count -eq 0) 'An unplugged iPhone disappears even during a copy'

    # 5. pnputil is busy from an earlier pass.
    $script:procs = @($helperProc)
    $script:PnputilProc = [pscustomobject]@{ HasExited = $false; ExitCode = 0 }
    $source = Update-DeviceCacheOnce
    $acq = Read-Acq
    Assert-True ($source -eq 'registry' -and [string]$acq.note -match 'pnputil\) is busy' -and @($acq.devices | Where-Object { $_.adapter -eq 'android_adb' }).Count -eq 1) 'A busy pnputil no longer freezes the list'
    $script:PnputilProc = $null

    # 6. A --verify check or the recovery script is not a copy in progress.
    $script:procs = @($helperProc,
        [pscustomobject]@{ ProcessId = 4700; ParentProcessId = $helperPid; Name = 'python.exe'; CommandLine = 'python.exe ios_usbmux_backup.py --verify E:\ib\x --since 1' },
        [pscustomobject]@{ ProcessId = 4800; ParentProcessId = $helperPid; Name = 'powershell.exe'; CommandLine = 'powershell.exe -File scripts\recover-stalled-ios-backup.ps1' })
    Assert-True (@(Get-UsbTransferProcesses).Count -eq 0) 'Verify runs and the recovery script do not pause the list'

    $text = [IO.File]::ReadAllText($helper)
    Assert-True ($text -match '\$script:HelperVersion = "5\.61"') 'Helper version 5.61'
    Write-Output "PASS: $script:checks device list refresh checks"
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
