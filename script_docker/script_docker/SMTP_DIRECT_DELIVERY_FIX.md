# Polaron: direct SMTP delivery only

The forensic API Compose service previously used `${MAIL_DELIVERY_HOST:-mailhog}` even when `.env` contained `MAIL_HOST=smtp.gmail.com`. It now honors `MAIL_HOST` and defaults to real Gmail SMTP rather than MailHog. A fail-closed guard rejects MailHog, port 1025 and placeholder credentials.

## Configure

In the project root `.env`, set:

```dotenv
MAIL_HOST=smtp.gmail.com
MAIL_PORT=587
MAIL_USERNAME=YOUR_GMAIL_ADDRESS
MAIL_PASSWORD=YOUR_GOOGLE_APP_PASSWORD
MAIL_FROM=YOUR_GMAIL_ADDRESS
MAIL_SMTP_AUTH=true
MAIL_SMTP_STARTTLS=true
MAIL_SMTP_SSL=false
MAIL_SEND_ENABLED=true
APP_BASE_URL=http://localhost:3001
```

Use a Google app password where eligible. Do not put secrets into source control. The old `MAIL_DELIVERY_*` override variables are no longer read by the API Compose service. Configure only the `MAIL_*` variables shown above.

`APP_BASE_URL` must be a publicly reachable HTTPS URL when inviting users from other devices. Localhost URLs only work on the host machine.

Run from PowerShell in project root:

```powershell
docker compose -p aetheris-forensic -f docker-compose.yml config --quiet
docker compose -p aetheris-forensic -f docker-compose.yml up -d --no-deps --force-recreate api
docker exec aetheris-forensic-api-1 printenv MAIL_HOST
docker exec aetheris-forensic-api-1 printenv MAIL_PORT
docker exec aetheris-forensic-api-1 printenv MAIL_SMTP_AUTH
```

Expected: `smtp.gmail.com`, `587`, `true`. Check `MAIL_USERNAME` is defined using `docker exec ... sh -c 'test -n "$MAIL_USERNAME"'`; do not print passwords. Ensure your network allows outbound port 587. Reissue any token that has been exposed in logs.

Note: successful SMTP submission to Gmail still does not guarantee recipient inbox placement. Check bounce notices, spam folders, and provider logs if needed.
