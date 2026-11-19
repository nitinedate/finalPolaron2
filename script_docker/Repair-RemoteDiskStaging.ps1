# Repair the known missing remote-disk-staging bind before Polaron startup.
# This script NEVER removes, renames, or overwrites a file or existing directory.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$composePath = Join-Path $ProjectRoot 'docker-compose.yml'
if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
    throw "Not a Polaron project root: $ProjectRoot (missing docker-compose.yml)"
}

$backupSetting = './backup'
$envFile = Join-Path $ProjectRoot '.env'
if (Test-Path -LiteralPath $envFile -PathType Leaf) {
    foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
        if ($line -match '^\s*(?:export\s+)?AETHERIS_BACKUP_DIR\s*=\s*(.*?)\s*$') {
            $candidate = [string]$Matches[1]
            # Simple Compose-style KEY=value, optional quotes and an inline comment.
            if ($candidate.StartsWith('"') -and $candidate.EndsWith('"')) {
                $candidate = $candidate.Substring(1, $candidate.Length - 2)
            } elseif ($candidate.StartsWith("'") -and $candidate.EndsWith("'")) {
                $candidate = $candidate.Substring(1, $candidate.Length - 2)
            } else {
                $candidate = ($candidate -split '\s+#', 2)[0].Trim()
            }
            if (-not [string]::IsNullOrWhiteSpace($candidate)) { $backupSetting = $candidate }
        }
    }
}

if ([System.IO.Path]::IsPathRooted($backupSetting)) {
    $backupRoot = [System.IO.Path]::GetFullPath($backupSetting)
    if (-not (Test-Path -LiteralPath $backupRoot -PathType Container)) {
        throw "Configured backup storage is unavailable: $backupRoot. Reconnect/restore it instead of creating an empty replacement."
    }
} else {
    $backupRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $backupSetting))
}
$staging = Join-Path $backupRoot 'forensic-data\remote-disk-staging'
if (Test-Path -LiteralPath $staging -PathType Leaf) {
    throw "Expected a remote staging directory but found a file: $staging"
}
if (-not (Test-Path -LiteralPath $staging -PathType Container)) {
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Write-Host "Created missing remote staging directory: $staging" -ForegroundColor Green
} else {
    Write-Host "Existing remote staging directory preserved: $staging" -ForegroundColor Green
}
if (-not (Test-Path -LiteralPath $staging -PathType Container)) {
    throw "Remote staging directory is still missing: $staging"
}
Write-Host 'Remote disk staging bind is ready. Restart with script_docker\start_docker_prod.cmd.' -ForegroundColor Green
