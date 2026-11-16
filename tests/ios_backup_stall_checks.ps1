# V1.31.4 offline checks: an iPhone backup that stops at 100% no longer hangs the collection.
# The backup tool is replaced by a fake that hangs like pymobiledevice3 does; --verify runs the real check.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }

. (Join-Path $ProjectRoot 'scripts/host-mobile-acquire.ps1')
$ErrorActionPreference = 'Continue'   # what native_ios_job.ps1 runs with

$tmp = Join-Path ([IO.Path]::GetTempPath()) ('polaron-ios-stall-' + [guid]::NewGuid().ToString('N'))
$fakeRepo = Join-Path $tmp 'repo'
New-Item -ItemType Directory -Force -Path (Join-Path $fakeRepo 'scripts') | Out-Null
$udid = '00008150-0002659C0CBB401C'
$python = (Get-Command python3 -ErrorAction Stop).Source
$realScript = Join-Path $ProjectRoot 'scripts/ios_usbmux_backup.py'
$builder = Join-Path $tmp 'make_backup.py'
@'
import plistlib, sqlite3, sys
from pathlib import Path
root = Path(sys.argv[1]); complete = sys.argv[2] == "complete"
root.mkdir(parents=True, exist_ok=True)
data = {"d7af800f89fbaf26992a5ce327b35fdec2876a17": b"chat" * 100, "3d0d7e5fb2ce288813306e4d4636395e047a3d28": b"sms" * 50}
for fid, blob in data.items():
    (root / fid[:2]).mkdir(exist_ok=True); (root / fid[:2] / fid).write_bytes(blob)
(root / "Manifest.plist").write_bytes(b"" if not complete else plistlib.dumps({"IsEncrypted": False, "Version": "10.0", "pad": "x" * 80}))
if complete:
    con = sqlite3.connect(root / "Manifest.db")
    con.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")
    for fid, blob in data.items():
        con.execute("INSERT INTO Files VALUES (?,?,?,?,?)", (fid, "AppDomain", fid, 1, None))
    con.execute("CREATE TABLE pad (x BLOB)"); con.execute("INSERT INTO pad VALUES (?)", (b"p" * 200,)); con.commit(); con.close()
'@ | Set-Content -LiteralPath $builder -Encoding UTF8

# The fake "ios_usbmux_backup.py": --verify -> the real check; otherwise behave per FAKE_BACKUP_MODE.
$fake = @"
import json, os, subprocess, sys, time
argv = sys.argv[1:]
open(os.environ['FAKE_ARGV_FILE'], 'w').write(json.dumps(argv))
if '--verify' in argv:
    sys.exit(subprocess.call([sys.executable, r'$realScript'] + argv))
staging = os.environ['FAKE_STAGING']
mode = os.environ['FAKE_BACKUP_MODE']
print(json.dumps({'stage': 'short_path', 'dest': staging}), flush=True)
subprocess.check_call([sys.executable, r'$builder', os.path.join(staging, '$udid'), 'complete' if mode != 'hang_incomplete' else 'partial'])
print(json.dumps({'progress': 100, 'pct': 100}), flush=True)
if mode == 'finish':
    print(json.dumps({'ok': True, 'backup_path': os.path.join(staging, '$udid'), 'files': 2}), flush=True)
    sys.exit(0)
time.sleep(3600)   # the iPhone never ends the session
"@
$fake | Set-Content -LiteralPath (Join-Path $fakeRepo 'scripts/ios_usbmux_backup.py') -Encoding UTF8

function Get-RepoRoot { return $fakeRepo }
# Same-volume commit (what Link-IosBackupIntoJob does when staging and case share a drive; its junction branch needs cmd.exe).
function Link-IosBackupIntoJob { param([string]$SourceBackup, [string]$DestBackup, [string]$LogPath) New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DestBackup) | Out-Null; Move-Item -LiteralPath $SourceBackup -Destination $DestBackup -Force; return (Test-IosBackupComplete -Path $DestBackup) }
function Find-IosKitPython { return $python }
function Get-KitPythonPathEnv { return '' }
function taskkill.exe { $id = [int]$args[1]; Get-Process -Id $id -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; Get-Process python3 -ErrorAction SilentlyContinue | Where-Object { $_.Parent.Id -eq $id } | Stop-Process -Force -ErrorAction SilentlyContinue }
$env:IOS_BACKUP_FINALIZE_STALL_MINUTES = '0.07'   # about 4 s here; 8 min by default
$env:FAKE_ARGV_FILE = Join-Path $tmp 'argv.json'

function Invoke-Case([string]$Mode, [switch]$Resume) {
    $case = Join-Path $tmp ([guid]::NewGuid().ToString('N'))
    $env:FAKE_STAGING = Join-Path $case 'ib/0CBB401C/20261116_180455'
    $env:FAKE_BACKUP_MODE = $Mode
    $out = Join-Path $case 'case/02_Original_Extraction/run'
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    $script:log = Join-Path $case 'ios_acquire.log'
    $script:progress = Join-Path $case 'progress.json'
    $started = Get-Date
    $r = Invoke-IosPymobileBackup -OutDir $out -LogPath $script:log -Udid $udid -BackupRoot (Join-Path $out 'ios_image') `
        -ProgressFile $script:progress -RunName 'run' -Stamp '20261116_180455' -Resume:$Resume
    $script:seconds = ((Get-Date) - $started).TotalSeconds
    return $r
}

try {
    $r = Invoke-Case 'hang_complete'
    $logText = Get-Content -LiteralPath $script:log -Raw
    Assert-True ($r.ok -eq $true) 'Stuck at 100% with the backup complete on disk: the collection continues'
    Assert-True ([string]$r.backup_path -like '*ios_image*00008150-0002659C0CBB401C' -and (Test-IosBackupComplete -Path $r.backup_path)) 'The verified backup is committed into the case image folder'
    Assert-True ($script:seconds -lt 60) ("It does not wait forever (took {0:N0} s)" -f $script:seconds)
    Assert-True ($logText -match 'No data from the iPhone for \d+ min at backup 100%' -and $logText -match 'Backup is complete on disk: Manifest.db lists 2 files, none missing') 'The log says what happened'
    $prog = Get-Content -LiteralPath $script:progress -Raw | ConvertFrom-Json
    Assert-True ([string]$prog.detail -match 'has sent nothing for .* while finishing the backup\. If it is locked or asleep, unlock it') 'While waiting, the page tells the examiner to unlock the iPhone'
    Assert-True (@(Get-Process python3 -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*ios_usbmux_backup.py*' -and $_.CommandLine -notlike '*--verify*' }).Count -eq 0) 'The stuck backup process is stopped'

    $r = Invoke-Case 'hang_incomplete'
    Assert-True ($r.ok -eq $false -and $r.error -eq 'ios_backup_stalled_no_data_from_iphone' -and $r.stalled -eq $true) 'Stuck before the backup is complete: retryable stall error'
    Assert-True ([string]$r.detail -match 'manifest_db_missing') 'The reason is reported'
    Assert-True ((Get-Content -LiteralPath $script:log -Raw) -match 'resumes into the same staging folder') 'The log says it will resume'

    $r = Invoke-Case 'finish' -Resume
    Assert-True ($r.ok -eq $true -and $script:seconds -lt 30) 'A backup that finishes normally is unaffected'
    $argv = Get-Content -LiteralPath $env:FAKE_ARGV_FILE -Raw | ConvertFrom-Json
    Assert-True (@($argv) -contains '--resume') 'A retry passes --resume to the backup tool'
    Assert-True ((Get-Content -LiteralPath $script:log -Raw) -notmatch 'No data from the iPhone') 'No stall reported for a normal backup'

    Assert-True ((Get-IosStallMinutes -Finishing $true) -eq 0.07) 'Finishing stall limit is read from IOS_BACKUP_FINALIZE_STALL_MINUTES'
    $env:IOS_BACKUP_FINALIZE_STALL_MINUTES = ''
    $env:IOS_BACKUP_STALL_MINUTES = ''
    Assert-True ((Get-IosStallMinutes -Finishing $true) -eq 8 -and (Get-IosStallMinutes -Finishing $false) -eq 25) 'Defaults: 8 min after 100%, 25 min during the backup'

    $job = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'scripts/native_ios_job.ps1'))
    Assert-True ($job -match '-Resume:\(\$tryN -gt 1\)') 'The job resumes on every retry'
    Write-Host "PASS: $script:checks iOS backup stall checks" -ForegroundColor Green
} finally {
    Get-Process python3 -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*$fakeRepo*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
