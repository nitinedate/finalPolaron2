# Polaron common Docker migration

The standalone reusable stack lives at `common-docker/`. It contains Postgres/pgvector, Redis, MinIO, OpenSearch, pgAdmin and MailHog. Neo4j remains Polaron-specific.

Polaron's existing `services/common/docker-compose.yml` stays supported by the PowerShell launcher and has been updated with shared OpenSearch. `docker-compose.yml` no longer creates OpenSearch; the forensic API and workers use `opensearch:9200` on the external `aetheris-common` network. Compose `depends_on: opensearch` entries were removed because Compose dependencies cannot cross project boundaries. Start the common services before Polaron.

**Do not run two OpenSearch containers over the same named volume**. The common OpenSearch reuses `aetheris-forensic_opensearch_data`. Migration must occur with the old container stopped. Back up data first.

When another project consumes common services, allocate dedicated DB user/database, Redis logical DB/queue prefix, MinIO credentials and bucket, and index namespace with access controls. Container separation alone is NOT tenant isolation. This package keeps legacy credentials as compatibility defaults; set stronger secrets on new deployments.

SuryaOCR remains primary; GLM-OCR is excluded. Ollama and Surya run on the host, not in common Docker. Vulnerability scanner components and security-sensitive malware analysis containers remain product-specific because they should not be casually shared. Neo4j stays product-owned.
