# Legacy OpenSearch -> Common Docker handover

Your legacy container `aetheris-forensic-opensearch-1` owns `aetheris-forensic_opensearch_data`, and Common Docker cannot mount the index until the legacy writer stops.

## Before handover

Schedule a maintenance window and confirm no forensic extraction/indexing is active. Take a verified backup/snapshot of the OpenSearch indices and Docker volume. This patch **does not** create a backup and cannot protect against host, disk or index corruption.

Confirm the exact current volume owner:

```powershell
docker ps --filter volume=aetheris-forensic_opensearch_data --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

Apply the patch at the existing project root, preserving its folder structure. Then execute **one time only**:

```cmd
cd /d D:\project1\polaran2
script_docker\start_docker_local.cmd -HandoverLegacyOpenSearch
```

For production:

```cmd
script_docker\start_docker_prod.cmd -HandoverLegacyOpenSearch
```

The `-HandoverLegacyOpenSearch` switch is explicit because this operation temporarily interrupts search/indexing. It:

1. Checks that the only non-Common owner of the volume is the expected `aetheris-forensic/opensearch` service.
2. Stops **only** that legacy OpenSearch container with a graceful 60-second limit.
3. Starts Common Docker OpenSearch using the same existing external Docker volume; it does not delete or copy the index.
4. Waits for Common OpenSearch to become healthy before continuing to stop/rebuild Polaron apps.
5. On startup or health failure, stops the new Common OpenSearch writer first, then attempts to restart the old container. If rollback cannot be guaranteed it explicitly fails instead of allowing two writers.

After a successful handover, use the normal `start_docker_local.cmd` or `start_docker_prod.cmd` with no flag. The legacy container remains stopped; retain it until you have verified data and completed the migration. The active forensic Compose configuration at `services/forensic/docker-compose.yml` does not define an OpenSearch service; it connects to Common Docker.

## Verify

```powershell
docker ps --filter volume=aetheris-forensic_opensearch_data --format "table {{.Names}}\t{{.Status}}"
docker ps --filter label=com.docker.compose.project=aetheris-common
Invoke-RestMethod http://127.0.0.1:19200/_cluster/health
```

Use the actual `COMMON_OPENSEARCH_PORT` stored in `.env` if the port changed. Also confirm expected index names and document counts before declaring success.

**Never** run `docker compose down -v`, `docker volume prune`, or start the old OpenSearch container while Common OpenSearch is running on the same volume. Do not apply the migration during an active case or an unverified backup state.

## Limitations

The changed Windows PowerShell startup logic and Docker Compose configuration were statically tested on Linux; an actual Docker Desktop / Windows handover has not been executed here.
