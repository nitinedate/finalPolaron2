# Polaron Shared OpenSearch Host Port Repair (2026-11-10)

## Root cause

A Windows or Docker process already occupies 127.0.0.1:9200. The Common Docker
OpenSearch container therefore cannot publish that host port. This does not mean
that Docker-internal port 9200 is unavailable. Previous `docker compose up -d`
also attempted to recreate healthy shared PostgreSQL and MinIO containers.

## Changes

- Default localhost OpenSearch host port is **19200** instead of 9200.
- The startup preflight selects an available port (19200-19250, 19300-19320)
  when the configured host port is busy, before Polaron application shutdown.
- The chosen port is stored in root `.env`; the original file is backed up.
- The default Docker-internal `OPENSEARCH_URL` is corrected to `http://opensearch:9200`.
- Common Docker starts only missing/stopped services. Already running shared
  PostgreSQL, Redis, MinIO, pgAdmin, OpenSearch and MailHog are not recreated.
- If an old stopped OpenSearch container still publishes the old port, only
  that container is recreated; its named data volume is preserved.
- A late Docker port-bind race gets one alternative-port retry.
- If a **legacy forensic OpenSearch** is still running on the same external index
  volume, startup aborts BEFORE stopping Polaron apps, rather than mounting
  the same index into two OpenSearch processes. Take a verified backup and
  stop that particular legacy server in a maintenance window before retrying.
- Both local/prod launchers use the same updated PowerShell library.

## Apply

Unzip the patch into `D:\project1\polaran2` preserving folder paths. Do not
replace existing `.env` or delete volumes. Then:

```cmd
cd /d D:\project1\polaran2
script_docker\start_docker_local.cmd
```

For production use `script_docker\start_docker_prod.cmd`. The initial startup
may still download images missing from the Docker cache, but cached packages
and Surya model files are reused.

### Verify after startup

```powershell
docker ps --filter label=com.docker.compose.project=aetheris-common
Invoke-RestMethod http://127.0.0.1:19200/_cluster/health
```

If the preflight picked another port, use the `COMMON_OPENSEARCH_PORT` value
shown in `D:\project1\polaran2\.env` instead of 19200.

### Troubleshooting

`Get-NetTCPConnection -State Listen -LocalPort 9200 -ErrorAction SilentlyContinue`
can show which host process owns 9200; this fix does **not** stop it.

The OpenSearch index data resides in the existing external volume
`aetheris-forensic_opensearch_data`, which this patch does not delete. The
`postgres-ensure` initialization runs only if it has not completed.

## Verification scope

Source-level tests and Compose YAML parse tests run in Linux. Actual Windows
Docker Desktop port allocation and multi-container startup require a run on
your workstation.

### Legacy index volume ownership (critical)

Run this command before migration when port 9200 belongs to another container:

```powershell
docker ps --filter volume=aetheris-forensic_opensearch_data --format "table {{.Names}}\t{{.Ports}}"
```

If it identifies a legacy OpenSearch service, do **not** run two copies using
that volume. Make a verified index backup, pause evidence jobs, then stop only
the identified legacy OpenSearch container during a maintenance window and
relaunch Polaron. Shared Common Docker will claim the existing volume; no
`down -v` or volume prune should be used.
