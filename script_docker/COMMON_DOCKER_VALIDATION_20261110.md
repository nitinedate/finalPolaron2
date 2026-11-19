# Common Docker extraction and validation — 10 November 2026

## Scope

Shared: PostgreSQL 16 with pgvector, Redis, MinIO, OpenSearch, pgAdmin, MailHog. Project-specific: Neo4j; malware antivirus/isolation and vulnerability-scanner toolchains; host-installed Ollama/Surya.

## Changes

- Added independently deployable `common-docker/` Compose, PowerShell and shell startup, env template, compatibility data mounts and guidance.
- Preserved the old `services/common/docker-compose.yml` entry point for Windows launchers, adding OpenSearch to it.
- Moved OpenSearch from the root forensic Compose into common Compose, using the legacy named data volume `aetheris-forensic_opensearch_data`.
- Removed invalid cross-project `depends_on: opensearch` entries; common infrastructure must start before the forensic product.
- Kept Neo4j inside forensic Compose.
- Updated launcher startup to create and start shared OpenSearch before dependent products, and removed OpenSearch from forensic startup waves.
- Changed product down routines to omit shared infrastructure.
- Preserved the existing MinIO bind directory when the `common-docker` directory is kept inside Polaron; moving the directory elsewhere requires explicit data path configuration.

## Offline validation

- PyYAML syntax verification on 29 Docker Compose files: passed (Compose-specific `!override` and `!reset` tags supported by validation loader).
- Root Compose schema test: 2 passed.
- Python compilation for `backend/app`: passed.
- Static topology assertions: passed; shared Compose has no Neo4j, forensic Compose has Neo4j and no OpenSearch, and product dependencies do not refer to external service dependencies.

## Not yet verified

- Docker Compose `config` evaluation and `up` on actual Docker host (Docker CLI not installed in test environment).
- Existing data migration across different Compose project names/volume names.
- Live OpenSearch health/readiness and indexing continuity.
- Shared service credential/security isolation for unrelated projects.
- Full forensic 100 GB performance and end-to-end Surya OCR.

## Migration safety

Make backups, stop the previous forensic-owned OpenSearch before starting the shared OpenSearch, verify `docker volume ls` and existing data paths. Never run two OpenSearch instances against the same volume, and never execute `docker compose down -v`. Existing forensic credentials are compatibility defaults, not suitable multi-tenant production defaults. Use per-project DB roles/databases, MinIO policies and bucket restrictions, Redis queue prefixes, and authenticated OpenSearch with tenant access controls before allowing untrusted projects on the shared network.
