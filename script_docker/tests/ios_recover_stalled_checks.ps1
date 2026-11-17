# V1.31.4 offline checks for scripts/recover-stalled-ios-backup.ps1 (Windows process and junction calls mocked).
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }

$env:POLARON_RECOVER_FUNCTIONS_ONLY = '1'
. (Join-Path $ProjectRoot 'scripts/recover-stalled-ios-backup.ps1')
$env:POLARON_RECOVER_FUNCTIONS_ONLY = ''
$ErrorActionPreference = 'Continue'

$udid = '00008150-0002659C0CBB401C'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('polaron-ios-recover-' + [guid]::NewGuid().ToString('N'))
$python = (Get-Command python3 -ErrorAction Stop).Source
function Get-RepoRoot { return $ProjectRoot }
function Find-IosKitPython { return $python }
function Get-KitPythonPathEnv { return '' }
function Write-Host { param([Parameter(ValueFromRemainingArguments = $true)]$Rest) $script:said += ,([string]($Rest | Where-Object { $_ -isnot [ConsoleColor] -and "$_" -notin @('-ForegroundColor','Red','Green','Yellow') } | Select-Object -First 1)) }

# Command-line parsing of the stuck process (what Win32_Process reports on the workstation).
function Get-CimInstance {
    @(
        [pscustomobject]@{ ProcessId = 4100; CreationDate = (Get-Date).AddHours(-2); CommandLine = '"C:\Polaron\tools\host-python\Scripts\python.exe" C:\Polaron\scripts\ios_usbmux_backup.py --udid 00008150-0002659C0CBB401C --dest E:\evidence\CASE-20261115-APPLEIPHON\02_Original_Extraction\RUN\ios_image --stamp 20261116_180455 --required-free-bytes 123' },
        [pscustomobject]@{ ProcessId = 4200; CreationDate = (Get-Date); CommandLine = 'python.exe C:\Polaron\scripts\ios_usbmux_backup.py --verify E:\ib\x --since 1' },
        [pscustomobject]@{ ProcessId = 4300; CreationDate = (Get-Date); CommandLine = 'python.exe something_else.py' }
    )
}
$found = @(Get-StuckIosBackupProcess)
Assert-True ($found.Count -eq 1 -and $found[0].ProcessId -eq 4100) 'Finds only the stuck backup process (not a --verify run or other Python)'
Assert-True ($found[0].Udid -eq $udid -and $found[0].Stamp -eq '20261116_180455' -and $found[0].Dest -eq 'E:\evidence\CASE-20261115-APPLEIPHON\02_Original_Extraction\RUN\ios_image') 'Reads UDID, stamp and case image folder from its command line'

function New-Scenario([string]$Kind) {
    $case = Join-Path $tmp ([guid]::NewGuid().ToString('N'))
    $staging = Join-Path $case 'ib/0CBB401C/20261116_180455'
    & $python -c @"
import importlib.util, sys
spec = importlib.util.spec_from_file_location('t', r'$ProjectRoot/backend/tests/test_ios_backup_stall_v1_31_4.py')
sys.path.insert(0, r'$ProjectRoot/backend')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from pathlib import Path
b = m.make_backup(Path(r'$staging'), m.FILES)
if '$Kind' == 'incomplete':
    (b / 'd7' / 'd7af800f89fbaf26992a5ce327b35fdec2876a17').unlink()
"@
    $script:proc = [pscustomobject]@{ ProcessId = 4100; Started = (Get-Date).AddMinutes(-5); Udid = $udid; Dest = (Join-Path $case 'case/ios_image'); Stamp = '20261116_180455' }
    $script:stagingDir = $staging
    $script:stopped = @(); $script:said = @()
    return $case
}
function Get-StuckIosBackupProcess { if ($script:proc) { return $script:proc } }
function Find-IosStaging { param([string]$Udid, [string]$Stamp) return $script:stagingDir }
function New-CaseLinkToBackup { param([string]$Link, [string]$Target) New-Item -ItemType SymbolicLink -Path $Link -Target $Target | Out-Null; return 'ok' }
function Stop-StuckBackup { param([int]$ProcessId) $script:stopped += $ProcessId }

try {
    $Staging = ''; $CaseImageFolder = ''; $CheckOnly = $false
    $case = New-Scenario 'complete'
    $code = Invoke-RecoverStalledIosBackup
    $link = Join-Path $case "case/ios_image/$udid"
    Assert-True ($code -eq 0) 'Complete backup: recovered'
    Assert-True ((Test-IosBackupComplete -Path $link) -and (Test-Path -LiteralPath (Join-Path $link 'd7/d7af800f89fbaf26992a5ce327b35fdec2876a17'))) 'The case image folder now points at the finished backup (no copy)'
    $pointer = Get-Content -LiteralPath (Join-Path $case 'case/ios_image/EXTERNAL_IOS_PAYLOAD.json') -Raw | ConvertFrom-Json
    Assert-True ($pointer.complete -and $pointer.payload_path -like '*20261116_180455*') 'Where the payload lives is recorded next to it'
    Assert-True ($script:stopped -contains 4100) 'The stuck process is stopped only after the link exists'

    $case = New-Scenario 'incomplete'
    $code = Invoke-RecoverStalledIosBackup
    Assert-True ($code -eq 2 -and -not (Test-Path -LiteralPath (Join-Path $case "case/ios_image/$udid")) -and $script:stopped.Count -eq 0) 'Incomplete backup: nothing changed, nothing stopped'
    Assert-True (@($script:said | Where-Object { $_ -match 'has NOT finished sending the backup \(files_missing_or_truncated; 1 of 3' }).Count -eq 1) 'Says why and what to do'

    $case = New-Scenario 'complete'
    $CheckOnly = $true
    $code = Invoke-RecoverStalledIosBackup
    Assert-True ($code -eq 0 -and -not (Test-Path -LiteralPath (Join-Path $case "case/ios_image/$udid")) -and $script:stopped.Count -eq 0) '-CheckOnly changes nothing'
    $CheckOnly = $false

    $script:proc = $null
    Assert-True ((Invoke-RecoverStalledIosBackup) -eq 1) 'No stuck backup running: says so'
    Write-Output "PASS: $script:checks recovery script checks"
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
