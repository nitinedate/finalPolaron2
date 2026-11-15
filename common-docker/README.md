# Common Docker infrastructure (independent of Polaron)

This directory can be copied into another repository or operated independently. It owns PostgreSQL 16 + pgvector, Redis, MinIO, OpenSearch, pgAdmin and MailHog. **Neo4j is deliberately not shared**, and remains in Polaron's forensic Compose stack.

## First deployment

1. Copy `.env.example` to `.env`, set strong passwords. Existing Polaron installations MUST retain established database and MinIO credentials; changing these env values does not rotate a populated PostgreSQL volume.
2. Run `./start.ps1` on Windows or `./start.sh` on Linux. This creates the external `aetheris-common` Docker network and reuses legacy named data volumes **without deleting or copying them**.
3. Start Polaron separately. Its Compose workloads join the external `aetheris-common` network.
4. For another application, attach its Compose service to the same external network and connect to `postgres:5432`, `redis:6379`, `minio:9000`, `opensearch:9200` and `mailhog:1025`.

Example:

```yaml
services:
  application:
    image: your-image
    networks: [default, shared-infra]
    environment:
      DATABASE_URL: postgresql://myapp_user:password@postgres:5432/myapp
      REDIS_URL: redis://redis:6379/3
      MINIO_ENDPOINT: minio:9000
      MINIO_BUCKET: myapp
networks:
  shared-infra:
    external: true
    name: aetheris-common
```

For genuine isolation, **create separate PostgreSQL users/databases and MinIO credentials/buckets for each app**, and use unique Redis database indexes/queue prefixes. `postgres-ensure` only initializes extensions in the configured bootstrap database; it does not automatically provision users or grant privileges. Make sure different applications cannot view one another's forensic evidence. OpenSearch has security disabled in the existing configuration and should be used only by trusted same-host applications; enable OpenSearch authentication/TLS and per-app indexes/roles before exposure to untrusted consumers. All published host ports are loopback-only. Keep host firewalls and backups in place.

## Existing data and compatibility

The standalone MinIO default is `../backup/minio` to preserve Polaron's original storage path when `common-docker/` is kept inside the project. If copied elsewhere, set `COMMON_MINIO_DATA_DIR` to the actual existing absolute data path before launching. Otherwise MinIO will appear empty although the old data still exists.

The named PostgreSQL, pgAdmin, and OpenSearch volumes retain their legacy names (`aetheris-forensic_*`) solely to reuse existing stored data. Redis uses the existing named volume. Do not invoke `docker compose down -v`, `docker volume rm`, or two common Compose configurations at the same time. `services/common/docker-compose.yml` is retained for Polaron's legacy launchers; it uses the same Compose project name, services, ports and named volumes as this standalone stack. Start **one** common stack configuration at a time; Polaron launchers can subsequently refer to their existing common Compose file. The root script expects its existing `.env` values for Polaron.

Before first migration from an active `aetheris-forensic` OpenSearch container: take a snapshot/backup, stop the forensic stack, and start common OpenSearch using the same volume `aetheris-forensic_opensearch_data`. Do not run old and new containers simultaneously against that data directory. Check `docker volume ls` for exact existing names before migration; volume identity can differ if a custom Compose project name was used.

**Limitations:** this is configuration-level integration. It has not been live-tested against your Docker host. The bundled setup scripts require validation on your workstation, particularly the transition to an independently managed shared OpenSearch and the secrets previously used in the existing installation.
