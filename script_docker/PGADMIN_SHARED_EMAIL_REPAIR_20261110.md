# Polaron / Common Docker pgAdmin email startup fix

**Issue:** pgAdmin continually logs:

```
email config is {'CHECK_EMAIL_DELIVERABILITY': False, 'ALLOW_SPECIAL_EMAIL_DOMAINS': ['local']}
'admin@example.test' does not appear to be a valid email address.
```

## Root cause

The prior root `.env.example` had `PGADMIN_DEFAULT_EMAIL=admin@example.test`, a reserved `.test` domain rejected by the pgAdmin container's `email-validator` even when email deliverability checks are disabled. On first launch the project's root `.env` was copied from that template. Later startup releases preserved `.env`, so changing only the template did not correct existing installations.

## Fixes in this release

1. Replace example default with `admin@forensic.example.com` in the root and Common Docker `.env.example` templates and three Compose definitions.
2. Add `scripts/ensure_pgadmin_email.ps1`. Both `start_docker_local.cmd` and `start_docker_prod.cmd` invoke this **through `script_docker/start_docker.ps1`** after first-launch configuration but before `-PrepareOnly`, `-ValidateOnly` and any Docker services.
3. Detect the exact known legacy `admin@example.test` value in the existing root `.env`, `common-docker/.env`, or inherited process environment, and replace only that value. Environment files are backed up alongside themselves before rewriting. Other email values are preserved; invalid custom ones produce actionable errors rather than silent rewrites.
4. Bring the standalone `common-docker/start.ps1` into the same Windows preflight, and make the Linux Common Docker startup fail fast with actionable guidance when it detects the old placeholder.
5. Include `script_docker/repair_pgadmin.cmd`, which fixes the settings and recreates **only** the pgAdmin service. No `docker down`, volume pruning, credential reset or database replacement occurs.
6. Preserve all earlier GPU caching, MinIO, SuryaOCR, specialist AI, evidence extraction and HTTPS startup optimizations.

## Applying the patch on Windows

Unzip **Polaron_PgAdmin_Email_Fix.zip** into `D:\project1\polaran2`, preserving its relative directories. It replaces several small configuration files and startup scripts. It does **not** include your `.env` files or evidence data.

For the quickest repair, run:

```cmd
cd /d D:\project1\polaran2
script_docker\repair_pgadmin.cmd
```

To start the whole project afterward:

```cmd
script_docker\start_docker_local.cmd
```

For production:

```cmd
script_docker\start_docker_prod.cmd
```

Or validate first:

```cmd
script_docker\start_docker_local.cmd -PrepareOnly
script_docker\start_docker_local.cmd -ValidateOnly
```

## Manual fix without downloading

Edit `D:\project1\polaran2\.env` and replace only the line:

```
PGADMIN_DEFAULT_EMAIL=admin@example.test
```

with:

```
PGADMIN_DEFAULT_EMAIL=admin@forensic.example.com
```

If you have a separate `common-docker\.env`, check the same key there. A `PGADMIN_DEFAULT_EMAIL` environment variable defined in your Windows terminal takes precedence over the `.env` file and must also be corrected or removed.

Then recreate **only pgAdmin** (from PowerShell):

```powershell
cd D:\project1\polaran2
docker compose --project-directory . -p aetheris-common -f services/common/docker-compose.yml up -d --no-deps --force-recreate pgadmin
docker compose --project-directory . -p aetheris-common -f services/common/docker-compose.yml logs --tail 40 pgadmin
```

## Important deployment details

- The new email is a syntactically valid **local login identifier**, not a mailbox or a claim about deliverability. Configure a real organization-controlled email for a production pgAdmin login.
- Existing pgAdmin accounts persist in its named Docker volume, so changing `PGADMIN_DEFAULT_EMAIL` does not rename an already-initialized account or reset its password. Continue logging in with the existing account until it is changed through the application's authorized administration workflow.
- Do **not** run `docker volume prune`, `docker compose down -v`, or delete `aetheris-forensic_pgadmin_data`.
- Preserve the `.env.pgadmin-backup-...` backup securely; it may contain passwords.
- The launcher fixes the known invalid value automatically, but does not automatically reset unrelated custom settings or credentials.
- A pre-existing unhealthy pgAdmin can force shared Compose reconciliation; healthy shared infrastructure containers are still not deliberately stopped by the fix.

## Validation scope

- pgAdmin default email accepted by Python `email-validator` with deliverability checks disabled.
- All three Compose definitions have the same valid fallback email.
- Standalone Linux script passes `sh -n`.
- Offline tests cover the email default, migration hook, file backups, Compose definitions, preserved volumes, existing entrypoints and pgAdmin-only repair command.
- No Windows PowerShell runtime, Docker Desktop, live pgAdmin database, or login testing was available here. Validate startup on your host using the commands above.
