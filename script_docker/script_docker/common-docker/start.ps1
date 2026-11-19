param([switch]$SkipEnsure)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$pgAdminPreflight = Join-Path (Split-Path -Parent $root) 'scripts/ensure_pgadmin_email.ps1'
if (Test-Path -LiteralPath $pgAdminPreflight -PathType Leaf) {
  & $pgAdminPreflight -ProjectRoot (Split-Path -Parent $root)
}
Push-Location $root
try {
  docker network inspect aetheris-common *> $null
  if ($LASTEXITCODE -ne 0) { docker network create aetheris-common | Out-Null }
  foreach ($name in @('aetheris-forensic_pgdata','aetheris-forensic_pgadmin_data','aetheris-forensic_opensearch_data')) {
    docker volume inspect $name *> $null
    if ($LASTEXITCODE -ne 0) { docker volume create $name | Out-Null }
  }
  docker compose -p aetheris-common up -d --wait postgres redis minio opensearch pgadmin mailhog
  if ($LASTEXITCODE -ne 0) { throw 'Shared services failed. Inspect docker compose logs.' }
  if (-not $SkipEnsure) {
    docker compose -p aetheris-common run --rm postgres-ensure
    if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed.' }
  }
} finally { Pop-Location }
