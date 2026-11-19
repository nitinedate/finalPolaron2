# Repair only the missing Polaron remote disk staging bind and its runtime allowlist.
# This script does NOT delete, stop, reset, or prune evidence, databases, or Docker volumes.
[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\project1\polaron\finalPolaron2'
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$lib = Join-Path $ProjectRoot 'script_docker\start_docker.lib.ps1'
$compose = Join-Path $ProjectRoot 'docker-compose.yml'
$launcher = Join-Path $ProjectRoot 'script_docker\start_docker.ps1'

if (-not (Test-Path -LiteralPath $compose -PathType Leaf)) {
    throw "Project root is incorrect: $ProjectRoot. docker-compose.yml was not found."
}
if (-not (Test-Path -LiteralPath $lib -PathType Leaf)) {
    throw "Startup library not found: $lib"
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Startup script not found: $launcher"
}

# This is the allowlist in the exact library loaded by script_docker/start_docker.ps1.
# Patch only that list; do not replace the whole script with a possibly older copy.
$source = [System.IO.File]::ReadAllText($lib)
$pattern = '(?s)\$runtimeTargets\s*=\s*@\((?<items>.*?)\)'
$match = [System.Text.RegularExpressions.Regex]::Match($source, $pattern)
if (-not $match.Success) {
    throw 'Cannot safely find $runtimeTargets in the active startup library; no code was modified.'
}
$items = $match.Groups['items'].Value
$remoteMount = "'/remote-disk-staging'"
if ($items.Contains($remoteMount)) {
    Write-Host "Startup validator already recognizes $remoteMount." -ForegroundColor Green
} else {
    if (-not $items.Contains("'/evidence'")) {
        throw 'Unrecognized runtime target list. Refusing to edit startup code automatically.'
    }
    $replacement = $items.Replace("'/evidence'", "'/evidence', '/remote-disk-staging'")
    $updated = $source.Substring(0, $match.Groups['items'].Index) + $replacement +
        $source.Substring($match.Groups['items'].Index + $match.Groups['items'].Length)
    # Preserve a copy of the exact existing startup library before the small change.
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$lib.pre-remote-staging-fix-$stamp.bak"
    Copy-Item -LiteralPath $lib -Destination $backup -ErrorAction Stop
    [System.IO.File]::WriteAllText($lib, $updated, ([System.Text.UTF8Encoding]::new($false)))
    Write-Host "Patched active validator: $lib" -ForegroundColor Green
    Write-Host "Saved original validator: $backup" -ForegroundColor DarkGray
}

# Resolve the backup root the same way relative Compose paths resolve against the project.
# A setting in the shell overrides .env in Docker Compose.
$backupSetting = $env:AETHERIS_BACKUP_DIR
if ([string]::IsNullOrWhiteSpace($backupSetting)) {
    $backupSetting = './backup'
    $envFile = Join-Path $ProjectRoot '.env'
    if (Test-Path -LiteralPath $envFile -PathType Leaf) {
        foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
            if ($line -match '^\s*(?:export\s+)?AETHERIS_BACKUP_DIR\s*=\s*(.*?)\s*$') {
                $candidate = [string]$Matches[1]
                if (($candidate.StartsWith('"') -and $candidate.EndsWith('"')) -or
                    ($candidate.StartsWith("'") -and $candidate.EndsWith("'"))) {
                    $candidate = $candidate.Substring(1, $candidate.Length - 2)
                } else {
                    $candidate = ($candidate -split '\s+#', 2)[0].Trim()
                }
                if (-not [string]::IsNullOrWhiteSpace($candidate)) { $backupSetting = $candidate }
            }
        }
    }
}
if ([System.IO.Path]::IsPathRooted($backupSetting)) {
    $backupRoot = [System.IO.Path]::GetFullPath($backupSetting)
    if (-not (Test-Path -LiteralPath $backupRoot -PathType Container)) {
        throw "Configured external backup storage is missing: $backupRoot. Reconnect/restore it; the repair will not replace it with an empty directory."
    }
} else {
    $backupRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $backupSetting))
}
$stage = Join-Path $backupRoot 'forensic-data\remote-disk-staging'
if (Test-Path -LiteralPath $stage -PathType Leaf) {
    throw "Remote staging mount is a FILE, not a directory: $stage. Inspect it manually; nothing was removed."
}
if (-not (Test-Path -LiteralPath $stage -PathType Container)) {
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Write-Host "Created missing runtime directory: $stage" -ForegroundColor Green
} else {
    Write-Host "Preserved existing runtime directory: $stage" -ForegroundColor Green
}
if (-not (Test-Path -LiteralPath $stage -PathType Container)) {
    throw "Remote staging directory is still unavailable: $stage"
}
$check = [System.IO.File]::ReadAllText($lib)
if (-not ([System.Text.RegularExpressions.Regex]::Match($check, $pattern).Groups['items'].Value.Contains($remoteMount))) {
    throw 'The startup validator could not be verified after repair.'
}
Write-Host ''
Write-Host 'PASS: Remote disk staging startup check is repaired.' -ForegroundColor Green
Write-Host "Project: $ProjectRoot"
Write-Host "Staging: $stage"
Write-Host ''
Write-Host 'Next command:' -ForegroundColor Cyan
Write-Host ('  & "{0}"' -f (Join-Path $ProjectRoot 'script_docker\start_docker_prod.cmd'))
