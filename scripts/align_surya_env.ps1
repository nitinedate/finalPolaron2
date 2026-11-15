# Upgrade known default Surya vLLM configuration to a native Windows host server.
# Custom admin-configured endpoints remain untouched.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ProjectRoot)
$ErrorActionPreference = 'Stop'
$path = Join-Path $ProjectRoot '.env'
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return }
$lines = @(Get-Content -LiteralPath $path -Encoding UTF8)
function Get-Setting([string]$key) {
    foreach ($line in $lines) {
        if ($line -match ('^\s*' + [regex]::Escape($key) + '=(.*)$')) { return $Matches[1].Trim().Trim('"').Trim("'") }
    }
    return ''
}
$mode = Get-Setting 'SURYA_HOST_MODE'
if (-not $mode -and $env:SURYA_HOST_MODE) { $mode = $env:SURYA_HOST_MODE }
$url = Get-Setting 'SURYA_INFERENCE_URL'
$old = 'http://host.docker.internal:8000/v1'
if (-not $mode -and $url -and $url -ne $old) { $mode = 'external' }
if (-not $mode) { $mode = 'native' }
if ($mode -notin @('native','external','disabled')) { throw "Unsupported SURYA_HOST_MODE '$mode'" }
$env:SURYA_HOST_MODE = $mode
if ($mode -ne 'native') { Write-Host "[Surya] Existing $mode configuration retained."; return }
$port = if ($env:SURYA_HOST_PORT) { [int]$env:SURYA_HOST_PORT } else { 8000 }
$updated = @()
$changes = 0
$required = @{
    'SURYA_INFERENCE_BACKEND' = 'llamacpp'
    'SURYA_INFERENCE_URL' = "http://host.docker.internal:$port/v1"
    'SURYA_HOST_MODE' = 'native'
}
foreach ($line in $lines) {
    $matched = $false
    foreach ($key in @($required.Keys)) {
        if ($line -match ('^\s*' + [regex]::Escape($key) + '=')) {
            $value = "$key=$($required[$key])"
            $updated += $value
            if ($line -ne $value) { $changes++ }
            $required.Remove($key)
            $matched = $true
            break
        }
    }
    if (-not $matched) { $updated += $line }
}
foreach ($key in $required.Keys) { $updated += "$key=$($required[$key])"; $changes++ }
if ($changes -gt 0) {
    $backup = "$path.surya-pre-native.bak"
    if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $path -Destination $backup }
    [System.IO.File]::WriteAllLines($path, [string[]]$updated, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[Surya] Updated $changes native settings; original backed up as $backup" -ForegroundColor Cyan
}
