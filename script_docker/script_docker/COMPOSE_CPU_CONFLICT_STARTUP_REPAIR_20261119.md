# Polaron local startup: conflicting Compose CPU limits (19 Nov 2026)

## Symptom

`start_docker_local.cmd` finishes the Windows drive scan, then fails with:

```
services.api: can't set distinct values on 'cpus' and 'deploy.resources.limits.cpus': invalid compose project
```

This is a **Compose configuration error**, not an extraction failure, drive-mount failure or reason to delete/rebuild forensic evidence. The successful `C, D, E` scan is not the cause.

## Cause and patch

Docker Compose defines both `services.<name>.cpus` and `services.<name>.deploy.resources.limits.cpus`. If both are present, they must have the **same** numeric value. CPU-safety updates previously used a mixture of styles, and an older/local Compose edit or merged file can introduce both styles with different values. The previously delivered sanitized source itself did not reproduce the exact `api` dual-value conflict; that implies the actual installed file or an additional overlay differs from the delivered ZIP.

This patch standardizes the edited Compose sources to **one authoritative field**:

```yaml
services:
  api:
    # No top-level cpus entry.
    deploy:
      resources:
        limits:
          cpus: "${FORENSIC_CPU_API_CPUS:-1.0}"
          memory: 4G
```

Updated paths:

- `docker-compose.yml`: 13 forensic CPU-limited services (including `api`, disk, native parser, OCR, RAG and progress).
- `services/vuln/docker-compose.yml`: central vulnerability API and dispatcher limits.
- `laptop-scanner/docker-compose.yml`: five Greenbone/on-site scanner CPU limits.
- `backend/tests/test_compose_cpu_limits_consistency_20261119.py`: regression tests for duplicate styles and preserved default budgets.

The original default limits and `FORENSIC_CPU_*`, `VULN_CPU_*` and `SCANNER_CPU_*` override variables are retained. No backend extraction logic, `.env`, database, evidence mount or generated drive overlay is changed. The budgets still represent individual-container limits, not a single host-wide 50% guarantee.

## Windows application steps

1. **Finish or checkpoint running forensic jobs first.** Back up the current project `docker-compose.yml` and any Compose files you have customized. Do not stop/delete PostgreSQL volumes or evidence, and do not run `docker system prune --volumes`.
2. From the *matching November 19 CPU-safety release*, extract `polaron_COMPOSE_CPU_CONFLICT_FIX_2026-11-19_PATCH.zip` **into the existing project root** `D:\project1\finalpolaron2\finalPolaron2`, preserving folder structure and overwriting the three Compose files. Do not replace your actual `.env` file.
3. Open PowerShell at the project root and run the **same forensic Compose validation** that stopped the launcher:

```powershell
Set-Location 'D:\project1\finalpolaron2\finalPolaron2'
docker compose --project-directory . --project-name aetheris-forensic `
  -f services/forensic/docker-compose.yml `
  -f docker-compose.drives.forensic.yml config --quiet
```

4. If that command exits without an error, start normally:

```powershell
.\script_docker\start_docker_local.cmd
```

5. If a conflict remains, a locally customized Compose file or extra override still contains both styles. To find CPU declarations:

```powershell
Select-String -Path '.\docker-compose.yml', '.\services\forensic\docker-compose.yml', '.\docker-compose.drives.forensic.yml' -Pattern '^\s*cpus\s*:'
```

For each affected service, remove the **service-level** `cpus:` key and keep its `deploy.resources.limits.cpus` setting. Do not delete the entire `deploy` block because it also contains memory limits and GPU reservations. The generated drive overlays should not be hand-edited: the launcher regenerates them.

## Verification and limits

- Offline parsing via PyYAML succeeds for all three edited Compose files.
- Seven regression assertions pass: field consistency, unchanged forensic/vulnerability/scanner default CPU budgets, and no second CPU limit in the forensic drive overlay.
- The updated package has not been run against Docker Desktop or the user's exact on-disk project, so **successful Windows Compose resolution and container startup require the validation command above**.
- A number of old, unrelated drive-mount tests still assume only one drive is generated and refer to removed helper functions; they fail against the inherited November 19 full source. This repair does not alter their scripts or expectations.

**If the `api` conflict reappears even after replacing `docker-compose.yml`, compare the checked-in and installed `services/forensic/docker-compose.yml` and any additional overrides**: a different included/merged Compose definition can still supply an extra CPU value.
