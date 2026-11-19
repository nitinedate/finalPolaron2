#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ -f .env ] && grep -Eiq '^PGADMIN_DEFAULT_EMAIL=.*admin@example[.]test' .env; then
  echo 'ERROR: common-docker/.env contains PGADMIN_DEFAULT_EMAIL=admin@example.test (reserved .test domain).' >&2
  echo 'Change it to admin@forensic.example.com, then retry. Existing pgAdmin volumes must not be deleted.' >&2
  exit 2
fi
docker network inspect aetheris-common >/dev/null 2>&1 || docker network create aetheris-common >/dev/null
for v in aetheris-forensic_pgdata aetheris-forensic_pgadmin_data aetheris-forensic_opensearch_data; do
  docker volume inspect "$v" >/dev/null 2>&1 || docker volume create "$v" >/dev/null
done
docker compose -p aetheris-common up -d --wait postgres redis minio opensearch pgadmin mailhog
docker compose -p aetheris-common run --rm postgres-ensure
