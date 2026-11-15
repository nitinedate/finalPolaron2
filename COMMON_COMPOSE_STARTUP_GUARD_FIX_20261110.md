# Polaron Windows startup: shared Compose directory guard correction

## Root cause

The local and production CMD files checked `common-docker/docker-compose.yml` even
though `script_docker/start_docker.ps1` loads the `common` Compose project via
`Get-StackComposeArgs` in `start_docker.lib.ps1`, which specifies
`services/common/docker-compose.yml`. The required-files manifest agrees with
`services/common`. A partial launcher update without the optional standalone
`common-docker` tree caused the CMD files to exit with code 2 before the
PowerShell startup validation could run.

## Fix

Both CMD launchers now check **services/common/docker-compose.yml**, the actual
runtime dependency. They no longer require the standalone `common-docker` tree.
The patch includes both full common stack layouts and their bind-mounted
configuration assets so a partial deployment can restore missing common files.

The normal cached build, SuryaOCR, pgAdmin, MinIO, GPU, and application-only stop
logic remain unchanged. No evidence, database volumes, local `.env` or credentials
are overwritten by the update.

## Install

Extract the patch ZIP into the project root, e.g. `D:\project1\polaran2`,
with *extract all* and preserving paths (`script_docker/`, `services/common/`,
`common-docker/`, `docker/pgadmin/` and `docker/postgres/`).

Then run:

```bat
cd /d D:\project1\polaran2
if exist services\common\docker-compose.yml echo Shared Compose file found
script_docker\start_docker_local.cmd -ValidateOnly
script_docker\start_docker_local.cmd
```

For HTTPS production, use `script_docker\start_docker_prod.cmd` instead. Validate
before restarting live forensic jobs.

**Important**: A file under `common-docker/` cannot simply be renamed to
`services/common/docker-compose.yml` without considering relative bind mount
paths. Both proper layouts are packaged with their dependent files.

## Validation boundaries

Source-level regression tests verify both CMD guards match the PowerShell runtime
Compose path, and check essential configuration files. Windows CMD, PowerShell,
Docker Desktop, active case jobs and network downloads cannot be executed in
this Linux packaging environment.
