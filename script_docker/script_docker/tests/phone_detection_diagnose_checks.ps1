# V1.31.7 offline checks: scripts\diagnose-phone-detection.ps1 reports why a phone is not listed.
# Runs the script against a stand-in helper (old version, other folder) and a saved pnputil output.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$script:checks = 0
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw "FAIL: $Message" }; $script:checks++ }

$diag = Join-Path $ProjectRoot 'scripts/diagnose-phone-detection.ps1'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('polaron-diag-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$port = Get-Random -Minimum 20000 -Maximum 40000
$server = Join-Path $tmp 'fake_helper.py'
@'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
BODIES = {
    "/health": {"ok": True, "helper_version": "5.61", "root": "C:\\OldPolaron"},
    "/acquisition/devices": {"ok": True, "devices": [{"adapter": "ios_lockdown", "os_family": "ios", "device_id": "x"}]},
    "/mobile-devices": {"ok": True, "mobile_devices": []},
}
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(BODIES.get(self.path, {"ok": False})).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'@ | Set-Content -LiteralPath $server
$pnp = Join-Path $tmp 'pnputil.txt'
@(
    'Microsoft PnP Utility',
    '',
    'Instance ID:                USB\VID_05AC&PID_12A8\00008150000265C0CBB401C',
    'Device Description:         Apple Mobile Device USB Composite Device',
    'Status:                     Started',
    'Instance ID:                USB\VID_2717&PID_FF48\a1b2c3d4',
    'Device Description:         USB Composite Device',
    'Status:                     Started',
    'Compatible IDs:             USB\COMPOSITE',
    'Instance ID:                USB\ROOT_HUB30\4&1234&0&0',
    'Device Description:         USB Root Hub (USB 3.0)',
    'Status:                     Started'
) | Set-Content -LiteralPath $pnp
$out = Join-Path $tmp 'report.txt'
$proc = Start-Process -FilePath 'python3' -ArgumentList @($server, "$port") -PassThru
try {
    $up = $false
    for ($i = 0; $i -lt 50 -and -not $up; $i++) {
        try { $null = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/health" -TimeoutSec 1; $up = $true } catch { Start-Sleep -Milliseconds 100 }
    }
    Assert-True $up 'The stand-in helper answers'
    $env:LOCALAPPDATA = $tmp
    $null = & $diag -Port $port -OutFile $out -PnputilOutput $pnp 6>$null
    $text = [IO.File]::ReadAllText($out); if ($env:DIAG_DEBUG) { Write-Host $text }
    $todo = ($text -split '=== 1\.')[0]
    Assert-True ($todo -match 'version 5\.61' -and $todo -match '5\.62' -and $todo -match 'ensure-host-drive-helper\.ps1 -ForceRestart') 'It says the running helper is too old and how to restart it'
    Assert-True ($todo -match 'starts from C:\\OldPolaron') 'It says when the helper runs from another folder'
    Assert-True ($todo -match 'only for charging' -and $todo -match 'File transfer') 'It finds the charging-only Android phone and says what to do'
    Assert-True ($text -match 'Apple Mobile Device USB Composite Device' -and $text -notmatch 'ROOT_HUB30') 'It lists the USB devices, without hubs'
    Assert-True ($text -match '"helper_version":\s*"5\.61"' -and $text -match 'ios_lockdown') "It records the helper's answer"
    $bytes = [IO.File]::ReadAllBytes($diag)
    Assert-True (@($bytes | Where-Object { $_ -gt 127 }).Count -eq 0) 'The script is plain ASCII (Windows PowerShell 5.1)'
    $src = [IO.File]::ReadAllText($diag)
    Assert-True ($src -notmatch '(?i)Stop-Process|\.Kill\(|Remove-Item -LiteralPath \$helper|Set-ItemProperty|New-ItemProperty|Restart-') 'It changes nothing on the computer'
    Write-Output "PASS: $script:checks phone detection diagnostic checks"
} finally {
    try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
