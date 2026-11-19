# Polaron complete updated project — 19 November 2026

This archive is a **complete project source bundle** assembled from the user-provided `polaron3(3).zip` plus the verified `polaron_fix_20261119_patch.zip`. It is NOT just a set of changed files.

## Components included

- Backend Python services, routers, workers, database schema and migrations, API configuration and tests
- React/TypeScript frontend and web assets
- Laptop vulnerability scanner agent, scanning services, scripts and client evidence agent
- Docker Compose definitions, service Dockerfiles, common Docker setup, deployment scripts, knowledge/report assets and historical technical docs
- Integrated 19-Nov-2026 fixes for remote E01 staging/progress diagnostics, vulnerability report site infrastructure and job selection, PDF Low exclusion and optional OCR model
- `POLARON_FIXES_20261119_README.md` for precise change list, configuration and validation scope
- `COMPLETE_PROJECT_SHA256_MANIFEST.json` listing package contents and SHA-256 hashes

## Safety and configuration

The source ZIP had a live `.env` and two root `.env.*.bak` backups. These are **intentionally omitted** from this shareable bundle to avoid redistributing secrets. The `.env.example` files are included; restore or create a valid `.env` in your private deployment environment before starting. Original evidence drives, case storage volumes, PostgreSQL data, OCR model weights, and installed dependencies are NOT embedded. These are persistent/runtime assets and should be preserved separately.

Only runtime Python bytecode (`__pycache__`, `*.pyc`) and pytest caches are also excluded. All other source files and documentation from the original archive are retained.

## Installation (Windows PowerShell)

Unzip into a new folder, e.g. `D:\Polaron_Complete_Updated`. Do not overwrite a running deployment or its `.env`/volumes blindly.

```powershell
Set-Location 'D:\Polaron_Complete_Updated'
# Copy a privately stored valid .env into the new root, or configure from .env.example
# Review the exact paths, bind mounts, and selected Compose overlays before starting.
docker compose config
# Start using your existing approved startup procedure after backups and verification.
```

## Tests run in this offline environment

- 19 passing targeted Python tests for updated report, remote progress, OCR, 1 environment skip.
- Previous wider targeted review: 73 passed, 1 skip, one older severity-test expectation deselected.
- Live PostgreSQL, GPU OCR inference, full npm install/build, client-to-server E01 extraction and two-laptop concurrent scanning were NOT exercised in this sandbox.

**Do not treat all code paths as production-verified** until those integration tests pass on the intended environment.
