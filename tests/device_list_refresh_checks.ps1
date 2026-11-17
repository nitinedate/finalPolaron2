# V1.31.6 offline checks: the workstation helper's phone list follows the cables.
# V1.31.7: Android phones are recognised by the USB interface they expose, and a phone that is plugged in
# but not usable yet (charging only, no Windows driver) is listed with what to do on it.
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
    if ($LiteralPath -eq 'HKLM:\SYSTEM\CurrentControlSet\Services') { return @(@('WinUSB', 'WUDFRd', 'usbccgp', 'USBAAPL64', 'disk') + @($script:running.Keys) | Select-Object -Unique | ForEach-Object { [pscustomobject]@{ PSPath = "svc::$_"; PSChildName = $_ } }) }
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
function Get-CimInstance { param($ClassName, $Filter, $OperationTimeoutSec, $ErrorAction) return $script:procs }
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


    # ---- V1.31.7 -------------------------------------------------------------------------------------
    $iphoneWpd = @{ Id = '\\?\usb#vid_05ac&pid_12a8#00008150000265c0cbb401c#{6ac27878-a6fa-4155-ba85-f98f491d4f33}'; Name = 'Apple iPhone' }
    function Get-AndroidRows { return @((Read-Acq).devices | Where-Object { $_.os_family -eq 'android' }) }

    # 7. The examiner's case: the iPhone is still plugged in and the Android phone is on "charging only".
    $xiaomi = 'USB\VID_2717&PID_FF48\a1b2c3d4'
    $script:enumProps[$xiaomi] = @{ DeviceDesc = '@usb.inf,%usb.compositedevice%;USB Composite Device'; Mfg = '@usb.inf,%generic.mfg%;(Standard USB Host Controller)'; CompatibleIDs = @('USB\DevClass_00&SubClass_00&Prot_00', 'USB\COMPOSITE') }
    $script:running = @{ usbccgp = @($xiaomi); USBAAPL64 = @($iphone) }; $script:wpd = @($iphoneWpd)
    $null = Update-DeviceCacheOnce
    $rows = @(Get-AndroidRows)
    Assert-True ($rows.Count -eq 1 -and $rows[0].adapter -eq 'android_usb' -and $rows[0].status -eq 'charging_only') 'A charging-only Android phone is listed (as needing action), not left out'
    Assert-True ([string]$rows[0].guidance -match 'File transfer' -and [string]$rows[0].guidance -match 'Rescan') 'It says what to do on the phone'
    Assert-True ([string]$rows[0].device_id -eq 'a1b2c3d4' -and [string]$rows[0].instance_id -eq $xiaomi) 'It is identified by its serial and USB instance (as with USB debugging)'
    Assert-True (@((Read-Acq).devices | Where-Object { $_.os_family -eq 'ios' }).Count -eq 1) 'The iPhone is still listed next to it'
    $mobileCache = @(Get-Content -LiteralPath $cacheFile -Raw | ConvertFrom-Json)
    Assert-True (@($mobileCache | Where-Object { $_.connection -eq 'usb' -and $_.state -eq 'charging_only' -and $_.live_acquire -eq $false }).Count -eq 1) 'The mobile list marks it as not collectable yet'

    # 8. File transfer on, but the phone is named only by a model code and its vendor ID is not a known phone maker.
    $tabDev = 'USB\VID_1F3A&PID_1007\X10SERIAL01'
    $tabMtp = 'USB\VID_1F3A&PID_1007&MI_00\7&1A2B3C&0&0000'
    $script:enumProps[$tabDev] = @{ DeviceDesc = '@usb.inf,%usb.compositedevice%;USB Composite Device'; CompatibleIDs = @('USB\COMPOSITE') }
    $script:enumProps[$tabMtp] = @{ DeviceDesc = '@wpdmtp.inf,%mtp.devicedesc%;X10 Pro'; CompatibleIDs = @('USB\MS_COMP_MTP', 'USB\Class_06&SubClass_01&Prot_01', 'USB\Class_06&SubClass_01', 'USB\Class_06') }
    $script:running = @{ usbccgp = @($tabDev); WUDFRd = @($tabMtp) }
    $script:wpd = @(@{ Id = '\\?\usb#vid_1f3a&pid_1007&mi_00#7&1a2b3c&0&0000#{6ac27878-a6fa-4155-ba85-f98f491d4f33}'; Name = 'X10 Pro' })
    $null = Update-DeviceCacheOnce
    $rows = @(Get-AndroidRows)
    Assert-True ($rows.Count -eq 1 -and $rows[0].adapter -eq 'android_mtp' -and $rows[0].label -eq 'X10 Pro') 'A File transfer phone with a model-code name and an unlisted vendor is listed'

    # 9. The same phone with no Windows portable-device entry: found by its File transfer interface.
    $script:wpd = @()
    $null = Update-DeviceCacheOnce
    $rows = @(Get-AndroidRows)
    Assert-True ($rows.Count -eq 1 -and $rows[0].adapter -eq 'android_mtp' -and $rows[0].label -eq 'X10 Pro' -and [string]$rows[0].device_id -eq $tabDev) 'A File transfer phone missing from the portable-device list is still listed'

    # 10. Things that are not phones stay out: a Samsung SSD, a Huawei USB modem, a camera (picture transfer).
    $ssd = 'USB\VID_04E8&PID_4001\0123456789AB'
    $modem = 'USB\VID_12D1&PID_1506&MI_00\7&2C1F&0&0000'
    $camera = 'USB\VID_04A9&PID_32F1\0123ABCD'
    $script:enumProps[$ssd] = @{ DeviceDesc = '@usbstor.inf,%uas%;Samsung Portable SSD T7'; CompatibleIDs = @('USB\Class_08&SubClass_06&Prot_62') }
    $script:enumProps[$modem] = @{ DeviceDesc = '@oem40.inf,%modem%;HUAWEI Mobile Connect - 3G Modem'; CompatibleIDs = @('USB\Class_ff&SubClass_02&Prot_01') }
    $script:enumProps[$camera] = @{ DeviceDesc = '@wpdmtp.inf,%ptp%;Canon EOS R6'; CompatibleIDs = @('USB\Class_06&SubClass_01&Prot_01') }
    $script:running = @{ UASPStor = @($ssd); ewser2k = @($modem); WUDFRd = @($camera) }
    $script:wpd = @(@{ Id = '\\?\usb#vid_04a9&pid_32f1#0123abcd#{6ac27878-a6fa-4155-ba85-f98f491d4f33}'; Name = 'Canon EOS R6' })
    $null = Update-DeviceCacheOnce
    Assert-True (@(Get-AndroidRows).Count -eq 0) 'An SSD, a USB modem and a camera are not listed as phones'

    # 11. What pnputil shows: a phone whose USB debugging interface has no Windows driver.
    $driverMissing = @(
        'Instance ID:                USB\VID_2717&PID_FF48\a1b2c3d4',
        'Device Description:         USB Composite Device',
        'Status:                     Started',
        'Compatible IDs:             USB\DevClass_00&SubClass_00&Prot_00',
        '                            USB\COMPOSITE',
        'Instance ID:                USB\VID_2717&PID_FF48&MI_01\7&1F2E3D&0&0001',
        'Device Description:         ADB Interface',
        'Status:                     Problem',
        'Problem Code:               28 (0x1C) [CM_PROB_FAILED_INSTALL]',
        'Compatible IDs:             USB\Class_ff&SubClass_42&Prot_01',
        '                            USB\Class_ff&SubClass_42')
    $s = Get-AndroidUsbStates -Lines $driverMissing
    Assert-True (@($s.Rows).Count -eq 1 -and $s.Rows[0].state -eq 'driver_missing' -and $s.Rows[0].guidance -match 'driver') 'A phone whose USB mode has no Windows driver is listed with that reason'

    # 12. What pnputil shows for a Samsung in File transfer mode: listed under the model name on its MTP interface.
    $samsung = @(
        'Instance ID:                USB\VID_04E8&PID_6860\R5CW12ABCDE',
        'Device Description:         SAMSUNG Mobile USB Composite Device',
        'Manufacturer Name:          SAMSUNG Electronics Co., Ltd.',
        'Status:                     Started',
        'Instance ID:                USB\VID_04E8&PID_6860&MI_00\7&3A4B5C&0&0000',
        'Device Description:         SM-A546E',
        'Status:                     Started',
        'Compatible IDs:             USB\MS_COMP_MTP',
        '                            USB\Class_06&SubClass_01&Prot_01')
    $s = Get-AndroidUsbStates -Lines $samsung
    Assert-True ($s.MtpKeys.Contains('04E8|6860') -and @($s.Rows).Count -eq 0) 'A Samsung in File transfer mode is ready, not needing action'
    Assert-True (@($s.Ready).Count -eq 1 -and $s.Ready[0].name -eq 'SM-A546E' -and $s.Ready[0].id -eq 'USB\VID_04E8&PID_6860\R5CW12ABCDE') 'It is named by its model and identified by its serial'

    # 13. A Samsung on charging only (no File transfer, no USB debugging) needs action.
    $s = Get-AndroidUsbStates -Lines @($samsung[0..3])
    Assert-True (@($s.Rows).Count -eq 1 -and $s.Rows[0].state -eq 'charging_only' -and $s.Rows[0].name -eq 'SAMSUNG Mobile USB Composite Device') 'A Samsung on charging only is listed under its USB name'

    # 14. Both copies of the classifier (startup scan and background refresh) are the same code.
    $fns = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-AndroidUsbStates' }, $true))
    $norm = @($fns | ForEach-Object { (($_.Extent.Text -split "`n") | ForEach-Object { $_.Trim() }) -join "`n" })
    Assert-True ($fns.Count -eq 2 -and $norm[0] -eq $norm[1]) 'The startup and background classifiers are identical'
    foreach ($builder in @('Get-PresentWpdPhones', 'Read-WpdPhones')) {
        $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $builder }, $true)
        $body = $fn.Extent.Text
        Assert-True ($body -match '\$usbStates = Get-AndroidUsbStates' -and $body -match '\$usbStates\.MtpKeys\.Contains' -and $body -match '@\(@\(\$usbStates\.Ready\) \+ @\(\$usbStates\.Rows\)\)') "$builder uses the Android USB classifier"
    }

    # ---- V1.31.9 ----------------------------------------------------------------------------------
    # 15. A failing stay-awake call no longer stops the list from being written (it threw first before).
    $adbOnly = 'USB\VID_18D1&PID_4EE7\8ABX0001'
    $script:enumProps[$adbOnly] = @{ DeviceDesc = '@oem1.inf,%adb%;ADB Device'; CompatibleIDs = @('USB\Class_ff&SubClass_42&Prot_01') }
    $script:running = @{ WinUSB = @($adbOnly) }; $script:wpd = @(); $script:procs = @()
    Remove-Item -LiteralPath $acqFile -Force -ErrorAction SilentlyContinue
    function Set-UsbStayAwake { param([bool]$On) throw 'Add-Type is blocked on this computer' }
    $null = Update-DeviceCacheOnce
    Assert-True (Test-Path -LiteralPath $acqFile) 'The list is written even when stay-awake fails'
    $rows = @((Read-Acq).devices)
    Assert-True ($rows.Count -eq 1 -and $rows[0].adapter -eq 'android_adb' -and $rows[0].label -eq 'Android phone') '"ADB Device" is not shown as the phone name'
    function Set-UsbStayAwake { param([bool]$On) }
    # 16. Nothing plugged in: the list is empty.
    $script:running = @{}; $script:wpd = @()
    $null = Update-DeviceCacheOnce
    Assert-True (@((Read-Acq).devices).Count -eq 0) 'Nothing plugged in: no phones'

    $text = [IO.File]::ReadAllText($helper)
    Assert-True ($text -match '\$script:HelperVersion = "5\.65"') 'Helper version 5.65'
    Assert-True ($text -match 'if \(\$null -eq \$context\) \{ Test-MobileDeviceRefresh; continue \}' -and $text -match 'function Test-MobileDeviceRefresh') 'The request loop restarts a stopped background refresh'
    Write-Output "PASS: $script:checks device list refresh checks"
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
