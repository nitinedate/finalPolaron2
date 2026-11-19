# Polaron frontend build hotfix — OCR search status (2026-11-19)

## What failed

Docker's unified frontend build (`tsc -b && vite build --mode unified`) stopped with:

`src/pages/forensic/OcrDocumentsPage.tsx(185,217): error TS2551: Property 'replaceAll' does not exist on type ...`

The frontend's `tsconfig.json` includes `ES2020` rather than `ES2021` in `compilerOptions.lib`. `String.prototype.replaceAll()` requires ES2021 type definitions. The subsequent `Build HTTPS gateway website` error was a **downstream build failure**, not evidence that Caddy or HTTPS routing is broken.

## Fix

In `frontend/src/pages/forensic/OcrDocumentsPage.tsx`, the OCR search status display now uses
`job.status.replace(/_/g, " ")` in place of `job.status.replaceAll("_", " ")`.

The replacement preserves the behavior for strings such as `completed_with_errors`, works under ES2020, and requires no dependency or compiler-target changes. An ES2020 regression test is provided in `tests/test_frontend_ocr_es2020_build.py`.

## Apply to the existing Windows installation

1. Stop the Polaron launch/rebuild attempt. Do not delete evidence, Docker volumes, PostgreSQL databases, or the private `.env`.
2. Extract `Polaron_Frontend_TS2551_Fix_20261119.zip` **into the project root** (`D:\project1\polaron\finalPolaron2`), preserving paths and replacing only the two paths shown in the patch ZIP.
3. In PowerShell:

```powershell
Set-Location 'D:\project1\polaron\finalPolaron2'
Select-String -Path '.\frontend\src\pages\forensic\OcrDocumentsPage.tsx' -Pattern 'job.status.replace'
Set-Location '.\script_docker'
.\start_docker_prod.cmd
```

The `Select-String` result should include `job.status.replace(/_/g, " ")`.

## Alternate manual one-line repair

```powershell
$path = 'D:\project1\polaron\finalPolaron2\frontend\src\pages\forensic\OcrDocumentsPage.tsx'
$content = Get-Content -Raw -LiteralPath $path
if (-not $content.Contains('job.status.replaceAll("_", " ")')) { throw 'Original expression not found: check file/path' }
$content = $content.Replace('job.status.replaceAll("_", " ")', 'job.status.replace(/_/g, " ")')
Set-Content -LiteralPath $path -Value $content -Encoding UTF8
```

## Validation limits

- Verified source change and exact front-end TS target.
- The isolated status expression typechecks with TypeScript under `--lib ES2020`.
- A full frontend `npm ci` / `npm run build:unified` could **not** be executed here because the npm registry is unreachable. The Windows Docker build remains the required end-to-end acceptance check.
- No live database, model, scanner, or evidence changes are included in this hotfix.
