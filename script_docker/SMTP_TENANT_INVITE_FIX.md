# Polaron: local SMTP tenant-invitation repair

## Problem
Tenant creation raised `MailDeliveryError: SMTP disconnected during authentication` when the identity API attempted to authenticate to local MailHog using Gmail-related settings inherited from `.env`.

## Changes
- `docker-compose.yml`: local `api` explicitly uses shared Docker `mailhog:1025`, without SMTP AUTH, STARTTLS or SSL. This overrides only the local API container and preserves production `docker-compose.https.yml` and its real mail-provider settings.
- `backend/app/services/email_service.py`: fail early with a clear configuration error if an SMTP AUTH/TLS setting is enabled for the MailHog hostname. Other SMTP providers remain unchanged.
- `backend/app/routers/tenants.py`: catch `MailDeliveryError`, roll back the pending transaction and return HTTP 503 with `invite_delivery_failed` instead of a raw 500 traceback. The invitation is not reported as sent when SMTP fails.

## Install
Extract patch at project root `D:\project1\polaran2` and preserve folder structure. Rebuild the forensic API (a container restart alone does not incorporate a Compose environment change unless Compose recreates the container). Existing cached dependency layers can be reused.

```
cd /d D:\project1\polaran2
script_docker\start_docker_local.cmd
```

## Verify
From Windows PowerShell:
```
docker exec aetheris-forensic-api-1 printenv MAIL_HOST
docker exec aetheris-forensic-api-1 printenv MAIL_SMTP_AUTH
docker exec aetheris-forensic-api-1 printenv MAIL_SMTP_STARTTLS
```
Expected: `mailhog`, `false`, `false`. Container name can vary; use `docker ps` to locate forensic API if necessary.

Browse local captured email at `http://localhost:8025/` (or `COMMON_MAILHOG_UI_PORT` from `.env`). MailHog does **not** deliver invitations to the real recipient; it captures them for development. For real delivery, configure a valid external SMTP service and authenticated credentials in the production profile.

## Tenant data
Existing tenant data and database volumes are not deleted. Check whether the tenant was created before retrying, since errors before this change may have affected the transaction. The new handler rolls back on mail delivery errors; database DDL is transactional under PostgreSQL in the normal transaction path.

## Validation limitations
Unit tests verify local SMTP configuration, SMTP send without AUTH/TLS, and controlled error handling. Live Windows Docker and real mail-provider delivery are not tested here.
