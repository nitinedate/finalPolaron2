# V1.31.8 offline checks: scripts\polaron-client-update.ps1 keeps a computer's Polaron files equal to the server's.
# Runs the real updater against a stand-in server (manifest + files, kit key checked) and a stand-in helper.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }

$updaterSrc = Join-Path $ProjectRoot 'scripts/polaron-client-update.ps1'
$pwsh = (Get-Process -Id $PID).Path
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('polaron-kit-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$serverPort = Get-Random -Minimum 20000 -Maximum 30000
$helperPort = Get-Random -Minimum 30001 -Maximum 40000
$state = Join-Path $tmp 'server-state.json'
$helperState = Join-Path $tmp 'helper-state.json'
$key = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718'

$serverPy = Join-Path $tmp 'fake_server.py'
@'
import base64, hashlib, json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
STATE, KEY, PORT = sys.argv[1], sys.argv[2], int(sys.argv[3])
class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        st = json.load(open(STATE))
        if self.headers.get("X-Polaron-Kit-Key") != KEY:
            return self.send(401, b'{"ok":false}')
        files = {p: base64.b64decode(c) for p, c in st["files"].items()}
        u = urlparse(self.path)
        if u.path == "/api/client-kit/manifest":
            rows = [{"path": p, "sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for p, b in files.items()]
            rows += st.get("extra", [])
            return self.send(200, json.dumps({"ok": True, "kit_version": st.get("kit", "k1"), "helper_version": "5.63", "files": rows}).encode())
        if u.path == "/api/client-kit/file":
            p = parse_qs(u.query).get("path", [""])[0]
            if p not in files:
                return self.send(404, b"{}")
            body = files[p] + (b"tampered" if p in st.get("tamper", []) else b"")
            return self.send(200, body, "application/octet-stream")
        self.send(404, b"{}")
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", PORT), H).serve_forever()
'@ | Set-Content -LiteralPath $serverPy
$helperPy = Join-Path $tmp 'fake_helper.py'
@'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
STATE, PORT = sys.argv[1], int(sys.argv[2])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        st = json.load(open(STATE))
        if self.path == "/acquisition/jobs":
            jobs = [{"job_id": "j1", "status": "running"}] if st.get("running") else []
            body = json.dumps({"ok": True, "jobs": jobs}).encode()
        else:
            body = json.dumps({"ok": True, "helper_version": "5.60", "port": PORT}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", PORT), H).serve_forever()
'@ | Set-Content -LiteralPath $helperPy

function B64([string]$s) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($s)) }
$updaterB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($updaterSrc))
function Set-ServerState([hashtable]$Files, [string[]]$Tamper = @(), [object[]]$Extra = @(), [string]$Kit = 'k1') {
    $f = @{ 'scripts/polaron-client-update.ps1' = $updaterB64 }
    foreach ($k in $Files.Keys) { $f[$k] = B64 $Files[$k] }
    [IO.File]::WriteAllText($state, (@{ files = $f; tamper = $Tamper; extra = $Extra; kit = $Kit } | ConvertTo-Json -Depth 5))
}
function Set-Helper([bool]$Running) { [IO.File]::WriteAllText($helperState, (@{ running = $Running } | ConvertTo-Json)) }
function Read-Text([string]$Path) { if (Test-Path -LiteralPath $Path) { return [IO.File]::ReadAllText($Path) } return $null }

$root = Join-Path $tmp 'client'
$cfg = Join-Path $tmp 'state/client-kit.json'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'scripts'), (Join-Path $root 'backend/app/services') | Out-Null
[IO.File]::WriteAllText((Join-Path $root 'scripts/host-drive-helper.ps1'), '$script:HelperVersion = "5.60"')
[IO.File]::WriteAllText((Join-Path $root 'scripts/keep.ps1'), 'same on both')
[IO.File]::WriteAllText((Join-Path $root '.env'), 'JWT_SECRET=client-secret')
(Get-Item (Join-Path $root 'scripts/keep.ps1')).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-3)
$keepTime = (Get-Item (Join-Path $root 'scripts/keep.ps1')).LastWriteTimeUtc

$serverFiles = @{
    'scripts/host-drive-helper.ps1'          = '$script:HelperVersion = "5.63"'
    'scripts/keep.ps1'                       = 'same on both'
    'scripts/ensure-host-drive-helper.ps1'   = '# ensure v5.63'
    'backend/app/services/phone_tools.py'    = 'VALUE = 1'
}
$sha = { param($s) ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s))) -replace '-', '').ToLowerInvariant() }
$extra = @(
    @{ path = '../evil.ps1'; sha256 = (& $sha 'x'); size = 1 },
    @{ path = '.env'; sha256 = (& $sha 'x'); size = 1 },
    @{ path = 'frontend/src/x.ts'; sha256 = (& $sha 'x'); size = 1 }
)
Set-ServerState -Files $serverFiles -Extra $extra
Set-Helper $false

$server = Start-Process -FilePath 'python3' -ArgumentList @($serverPy, $state, $key, "$serverPort") -PassThru
$helper = Start-Process -FilePath 'python3' -ArgumentList @($helperPy, $helperState, "$helperPort") -PassThru
try {
    foreach ($port in @($serverPort, $helperPort)) {
        $up = $false
        for ($i = 0; $i -lt 50 -and -not $up; $i++) {
            try { $null = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/" -TimeoutSec 1 -SkipHttpErrorCheck; $up = $true } catch { Start-Sleep -Milliseconds 100 }
        }
        Assert-True $up "Stand-in on port $port answers"
    }
    $base = "http://127.0.0.1:$serverPort"
    function Invoke-Updater([string[]]$UpdaterArgs, [string]$Script = $updaterSrc) {
        $null = & $pwsh -NoProfile -File $Script @UpdaterArgs -ConfigFile $cfg -HelperPort $helperPort 2>&1
        return $LASTEXITCODE
    }

    # 1. Setup: only differing files are downloaded, verified and written; nothing outside scripts\ and backend\app\.
    $code = Invoke-Updater @('-Server', $base, '-KitKey', $key, '-Service', 'mobile-android', '-Root', $root, '-Setup', '-NoHelperRestart')
    Assert-True ($code -eq 0) "Setup succeeds (exit $code)"
    Assert-True ((Read-Text (Join-Path $root 'scripts/host-drive-helper.ps1')) -eq '$script:HelperVersion = "5.63"') 'The old helper is replaced by the server copy'
    Assert-True ((Read-Text (Join-Path $root 'backend/app/services/phone_tools.py')) -eq 'VALUE = 1') 'A new Python module is added under backend\app'
    Assert-True ((Read-Text (Join-Path $root 'scripts/polaron-client-update.ps1')) -ne $null) 'The updater itself is installed for the watchdog'
    Assert-True ((Get-Item (Join-Path $root 'scripts/keep.ps1')).LastWriteTimeUtc -eq $keepTime) 'An identical file is not rewritten'
    Assert-True ((Read-Text (Join-Path $root '.env')) -eq 'JWT_SECRET=client-secret') '.env is never touched'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $tmp 'evil.ps1')) -and -not (Test-Path -LiteralPath (Join-Path $root 'frontend'))) 'Paths outside scripts\ and backend\app\ in the list are ignored'
    $backups = @(Get-ChildItem -LiteralPath (Join-Path $root '.polaron-update') -Directory -Filter 'backup-*')
    Assert-True ($backups.Count -eq 1 -and (Read-Text (Join-Path $backups[0].FullName 'scripts/host-drive-helper.ps1')) -eq '$script:HelperVersion = "5.60"') 'The replaced helper is kept in a backup'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $root '.polaron-update/staging'))) 'No staging folder is left behind'
    $c = Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json
    Assert-True ($c.server -eq $base -and $c.kit_key -eq $key -and $c.service -eq 'mobile-android' -and $c.root -eq [IO.Path]::GetFullPath($root) -and $c.kit_version -eq 'k1') 'The computer remembers its server for automatic updates'

    # 2. Running setup again changes nothing.
    $code = Invoke-Updater @('-Root', $root, '-Setup', '-NoHelperRestart')
    Assert-True ($code -eq 0) 'A second setup run is a no-op and reuses the saved server'

    # 3. Watchdog: checks at most every 10 minutes, then applies a server change and reports it (exit 10).
    $installed = Join-Path $root 'scripts/polaron-client-update.ps1'
    $serverFiles['backend/app/services/phone_tools.py'] = 'VALUE = 2'
    Set-ServerState -Files $serverFiles -Kit 'k2'
    $code = Invoke-Updater @('-Watchdog') $installed
    Assert-True ($code -eq 0 -and (Read-Text (Join-Path $root 'backend/app/services/phone_tools.py')) -eq 'VALUE = 1') 'The watchdog does not check again within 10 minutes'
    $c = Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json
    $c.checked_utc = [DateTime]::UtcNow.AddMinutes(-11).ToString('o')
    [IO.File]::WriteAllText($cfg, ($c | ConvertTo-Json))
    $code = Invoke-Updater @('-Watchdog') $installed
    Assert-True ($code -eq 10 -and (Read-Text (Join-Path $root 'backend/app/services/phone_tools.py')) -eq 'VALUE = 2') 'The watchdog applies a server change (exit 10)'
    Assert-True ((Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json).kit_version -eq 'k2') 'The new kit version is recorded'

    # 4. Never during a collection: the update waits (exit 11) and nothing changes.
    $serverFiles['scripts/host-drive-helper.ps1'] = '$script:HelperVersion = "5.64"'
    Set-ServerState -Files $serverFiles -Kit 'k3'
    Set-Helper $true
    $c = Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json; $c.checked_utc = [DateTime]::UtcNow.AddHours(-1).ToString('o'); [IO.File]::WriteAllText($cfg, ($c | ConvertTo-Json))
    $code = Invoke-Updater @('-Watchdog') $installed
    Assert-True ($code -eq 11 -and (Read-Text (Join-Path $root 'scripts/host-drive-helper.ps1')) -eq '$script:HelperVersion = "5.63"') 'A running collection postpones the update (exit 11)'
    $code = Invoke-Updater @('-Root', $root, '-Setup', '-NoHelperRestart')
    Assert-True ($code -eq 11 -and (Read-Text (Join-Path $root 'scripts/host-drive-helper.ps1')) -eq '$script:HelperVersion = "5.63"') 'Setup also waits for a running collection'
    Set-Helper $false

    # 5. A download that does not match the server list changes nothing (exit 3).
    Set-ServerState -Files $serverFiles -Kit 'k3' -Tamper @('scripts/host-drive-helper.ps1')
    $c = Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json; $c.checked_utc = [DateTime]::UtcNow.AddHours(-1).ToString('o'); [IO.File]::WriteAllText($cfg, ($c | ConvertTo-Json))
    $code = Invoke-Updater @('-Watchdog') $installed
    Assert-True ($code -eq 3 -and (Read-Text (Join-Path $root 'scripts/host-drive-helper.ps1')) -eq '$script:HelperVersion = "5.63"') 'A file that fails verification is not applied (exit 3)'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $root '.polaron-update/staging'))) 'The rejected download is removed'

    # 6. Wrong key or unreachable server: exit 2, nothing changed.
    $code = Invoke-Updater @('-Server', $base, '-KitKey', ('0' * 48), '-Root', $root, '-Setup', '-NoHelperRestart')
    Assert-True ($code -eq 2) 'A wrong kit key is refused (exit 2)'

    # 7. A computer that was never set up is left alone by the watchdog.
    $null = & $pwsh -NoProfile -File $installed -Watchdog -ConfigFile (Join-Path $tmp 'none/client-kit.json') -HelperPort $helperPort 2>&1
    Assert-True ($LASTEXITCODE -eq 0) 'No saved server: the watchdog does nothing'

    # 8. The script stays runnable by Windows PowerShell 5.1.
    $bytes = [IO.File]::ReadAllBytes($updaterSrc)
    Assert-True (@($bytes | Where-Object { $_ -gt 127 }).Count -eq 0) 'The updater is plain ASCII'
    Write-Output "PASS: $script:checks client kit update checks"
} finally {
    foreach ($p in @($server, $helper)) { try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {} }
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
