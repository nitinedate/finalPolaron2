# Repair a mixed-version Polaron frontend without deleting the previous work.
# Windows PowerShell 5.1+; no Python, npm or external utilities required.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ReleaseZip,
    [string] $ProjectRoot = (Get-Location).Path,
    [switch] $Apply
)

$ErrorActionPreference = 'Stop'
$release = (Resolve-Path -LiteralPath $ReleaseZip).Path
$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$frontend = Join-Path $project 'frontend'
$existingSource = Join-Path $frontend 'src'
if (-not (Test-Path -LiteralPath $existingSource -PathType Container)) {
    throw "Not a Polaron project root: expected $existingSource"
}
if (-not $release.EndsWith('.zip', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'ReleaseZip must be a complete source ZIP.'
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($release)
try {
    $entries = @($zip.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
    $required = @(
        'frontend/src/main.tsx',
        'frontend/src/App.tsx',
        'frontend/src/pages/forensic/MobileAcquisitionPage.tsx',
        'frontend/tsconfig.json',
        'frontend/package.json',
        'frontend/package-lock.json'
    )
    foreach ($path in $required) {
        if ($entries -notcontains $path) { throw "The selected ZIP is not a complete matching Polaron release; missing $path" }
    }
    $cleanSourceFiles = @($entries | Where-Object { $_.StartsWith('frontend/src/') -and -not $_.EndsWith('/') } | ForEach-Object { $_.Substring('frontend/src/'.Length) })
} finally { $zip.Dispose() }

$previousFiles = @(Get-ChildItem -LiteralPath $existingSource -Recurse -File | ForEach-Object {
    $_.FullName.Substring($existingSource.Length).TrimStart('\', '/').Replace('\', '/')
})
$extraFiles = @($previousFiles | Where-Object { $cleanSourceFiles -notcontains $_ } | Sort-Object)
Write-Host "Selected coherent release: $release"
Write-Host "Current frontend files: $($previousFiles.Count); release files: $($cleanSourceFiles.Count)"
Write-Host "Files present on this PC but absent from the selected release: $($extraFiles.Count)"
foreach ($item in ($extraFiles | Select-Object -First 30)) { Write-Host "  NOT IN RELEASE: $item" }
if ($extraFiles.Count -gt 30) { Write-Host "  ... $($extraFiles.Count - 30) further file(s)" }
Write-Warning 'Applying this release replaces ONLY the frontend source/config with the coherent version. Any newer UI features not in the release will be absent from the restored frontend, but preserved in the backup.'
if (-not $Apply) {
    Write-Host 'DRY RUN complete. Nothing changed. To apply, rerun with -Apply.'
    return
}

# Stage fully before touching the working frontend. Keep backup OUTSIDE the Docker build context.
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ('PolaronFrontStage-' + [guid]::NewGuid().ToString('N'))
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path (Split-Path -Parent $project) ('PolaronFrontendBackup-' + $stamp + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$filesToReplace = @('tsconfig.json', 'package.json', 'package-lock.json', 'vite.config.ts')
$sourceMoved = $false
try {
    New-Item -ItemType Directory -Path $stage | Out-Null
    Expand-Archive -LiteralPath $release -DestinationPath $stage -Force
    $stagedFrontend = Join-Path $stage 'frontend'
    foreach ($path in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $stage ($path.Replace('/', '\'))))) {
            throw "Release failed integrity check after extraction: $path"
        }
    }
    New-Item -ItemType Directory -Path $backup | Out-Null
    # Never remove local work; move the entire old source into the backup.
    Move-Item -LiteralPath $existingSource -Destination (Join-Path $backup 'src')
    $sourceMoved = $true
    foreach ($name in $filesToReplace) {
        $destination = Join-Path $frontend $name
        if (Test-Path -LiteralPath $destination) {
            Copy-Item -LiteralPath $destination -Destination (Join-Path $backup $name) -Force
        }
    }
    Copy-Item -LiteralPath (Join-Path $stagedFrontend 'src') -Destination $existingSource -Recurse
    foreach ($name in $filesToReplace) {
        $candidate = Join-Path $stagedFrontend $name
        if (Test-Path -LiteralPath $candidate) {
            Copy-Item -LiteralPath $candidate -Destination (Join-Path $frontend $name) -Force
        }
    }
    foreach ($item in @('tsconfig.tsbuildinfo', 'dist')) {
        $cache = Join-Path $frontend $item
        if (Test-Path -LiteralPath $cache) { Remove-Item -LiteralPath $cache -Recurse -Force }
    }
    Write-Host "Coherent frontend installed. BACKUP preserved at: $backup"
    Write-Host 'Next: docker compose -f docker-compose.https.yml build frontend'
} catch {
    Write-Warning "Frontend replacement failed: $($_.Exception.Message)"
    if ($sourceMoved -and (Test-Path -LiteralPath (Join-Path $backup 'src'))) {
        if (Test-Path -LiteralPath $existingSource) {
            Remove-Item -LiteralPath $existingSource -Recurse -Force
        }
        Move-Item -LiteralPath (Join-Path $backup 'src') -Destination $existingSource
    }
    foreach ($name in $filesToReplace) {
        $old = Join-Path $backup $name
        if (Test-Path -LiteralPath $old) {
            Copy-Item -LiteralPath $old -Destination (Join-Path $frontend $name) -Force
        }
    }
    throw
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
}
