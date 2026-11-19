# Windows shared OpenSearch host-port preflight for Polaron.
# Run before stopping application containers. Never terminate a foreign listener,
# recreate a running shared service, or remove any Docker volume.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [switch]$ForceAlternate,
    [int]$ExcludePort = 0
)
$ErrorActionPreference = 'Stop'
$envFile = Join-Path $ProjectRoot '.env'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw "[Common/OpenSearch] Missing root .env: $envFile"
}
function Get-EnvSetting([string]$Text, [string]$Name) {
    $escaped = [regex]::Escape($Name)
    $found = [regex]::Matches($Text, "(?m)^[ \t]*$escaped[ \t]*=[ \t]*([^\r\n#]*)")
    if ($found.Count -lt 1) { return '' }
    return $found[$found.Count - 1].Groups[1].Value.Trim().Trim('"').Trim("'")
}
function Test-LoopbackPortFree([int]$Port) {
    $sock = [System.Net.Sockets.Socket]::new([System.Net.Sockets.AddressFamily]::InterNetwork, [System.Net.Sockets.SocketType]::Stream, [System.Net.Sockets.ProtocolType]::Tcp)
    try {
        $sock.ExclusiveAddressUse = $true
        $ep = [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Loopback, $Port)
        $sock.Bind($ep)
        return $true
    } catch { return $false }
    finally { $sock.Dispose() }
}
function Save-RootEnvSetting([string]$Path, [string]$Text, [string]$Key, [string]$Value) {
    if ([regex]::IsMatch($Text, "(?m)^[ \t]*$([regex]::Escape($Key))[ \t]*=")) {
        return [regex]::Replace($Text, "(?m)^[ \t]*$([regex]::Escape($Key))[ \t]*=[^\r\n]*", "$Key=$Value")
    }
    $eol = if ($Text.Contains("`r`n")) { "`r`n" } else { "`n" }
    return $Text.TrimEnd("`r", "`n") + $eol + "$Key=$Value" + $eol
}
$text = [System.IO.File]::ReadAllText($envFile)
$desiredText = if ($env:COMMON_OPENSEARCH_PORT) { $env:COMMON_OPENSEARCH_PORT } else { Get-EnvSetting $text 'COMMON_OPENSEARCH_PORT' }
if (-not $desiredText) { $desiredText = '19200' }
if ($desiredText -notmatch '^\d{2,5}$' -or [int]$desiredText -lt 1024 -or [int]$desiredText -gt 65535) {
    throw "[Common/OpenSearch] Invalid COMMON_OPENSEARCH_PORT='$desiredText'. Choose a port from 1024 to 65535."
}
$desired = [int]$desiredText
$running = @(& docker ps -q --filter 'label=com.docker.compose.project=aetheris-common' --filter 'label=com.docker.compose.service=opensearch' 2>$null)
if ($LASTEXITCODE -ne 0) { throw '[Common/OpenSearch] Cannot inspect current Docker OpenSearch state.' }
if ($running.Count -gt 0 -and $running[0] -and -not $ForceAlternate) {
    # An existing running shared service owns its published port; preserve it.
    # A legacy OPENSEARCH_URL=localhost is still corrected for Docker clients.
    $oldUrl = Get-EnvSetting $text 'OPENSEARCH_URL'
    if ($oldUrl -in @('http://localhost:9200', 'http://127.0.0.1:9200')) {
        $newText = Save-RootEnvSetting $envFile $text 'OPENSEARCH_URL' 'http://opensearch:9200'
        $backup = "$envFile.opensearch.$((Get-Date).ToString('yyyyMMddHHmmss')).bak"
        Copy-Item -LiteralPath $envFile -Destination $backup -ErrorAction Stop
        [System.IO.File]::WriteAllText($envFile, $newText, ([System.Text.UTF8Encoding]::new($false)))
        Write-Host '[Common/OpenSearch] Corrected Docker client URL; existing service and port are unchanged.' -ForegroundColor Yellow
    }
    if ($env:OPENSEARCH_URL -in @('http://localhost:9200', 'http://127.0.0.1:9200')) {
        $env:OPENSEARCH_URL = 'http://opensearch:9200'
    }
    Write-Host '[Common/OpenSearch] Reusing existing running OpenSearch. No host port changes.' -ForegroundColor Green
    return
}
$chosen = $desired
if ($ForceAlternate -or $desired -eq $ExcludePort -or -not (Test-LoopbackPortFree $desired)) {
    $chosen = 0
    foreach ($candidate in @(19200..19250) + @(19300..19320)) {
        if ($candidate -eq $ExcludePort) { continue }
        if (Test-LoopbackPortFree $candidate) { $chosen = $candidate; break }
    }
    if (-not $chosen) {
        throw '[Common/OpenSearch] Could not find a free loopback port in 19200-19250 or 19300-19320. Free a port or set COMMON_OPENSEARCH_PORT in .env.'
    }
    Write-Host "[Common/OpenSearch] Host TCP $desired is in use; selecting $chosen (Docker-internal OpenSearch remains opensearch:9200)." -ForegroundColor Yellow
}
$changed = $false
$currentPort = Get-EnvSetting $text 'COMMON_OPENSEARCH_PORT'
if ($currentPort -ne [string]$chosen) {
    $text = Save-RootEnvSetting $envFile $text 'COMMON_OPENSEARCH_PORT' ([string]$chosen)
    $changed = $true
}
# Legacy localhost URL is not reachable from other containers. Keep custom endpoints.
$currentUrl = Get-EnvSetting $text 'OPENSEARCH_URL'
if ($currentUrl -in @('http://localhost:9200', 'http://127.0.0.1:9200')) {
    $text = Save-RootEnvSetting $envFile $text 'OPENSEARCH_URL' 'http://opensearch:9200'
    $changed = $true
    Write-Host '[Common/OpenSearch] Corrected old container URL to http://opensearch:9200.' -ForegroundColor Yellow
}
if ($changed) {
    $backup = "$envFile.opensearch.$((Get-Date).ToString('yyyyMMddHHmmss')).bak"
    Copy-Item -LiteralPath $envFile -Destination $backup -ErrorAction Stop
    [System.IO.File]::WriteAllText($envFile, $text, ([System.Text.UTF8Encoding]::new($false)))
    Write-Host '[Common/OpenSearch] Saved changed settings in root .env; original backed up locally.' -ForegroundColor Green
}
# Process environment takes precedence over .env in Docker Compose.
$env:COMMON_OPENSEARCH_PORT = [string]$chosen
if ($env:OPENSEARCH_URL -in @('http://localhost:9200', 'http://127.0.0.1:9200')) {
    $env:OPENSEARCH_URL = 'http://opensearch:9200'
}
Write-Host "[Common/OpenSearch] Host binding 127.0.0.1:$chosen; internal Docker URL http://opensearch:9200." -ForegroundColor Green
