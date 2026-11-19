# Real email delivery via Gmail SMTP

The default forensic API uses MailHog, which only captures messages. For external delivery edit the project root `.env` (never paste an App Password into chat):

```dotenv
MAIL_DELIVERY_HOST=smtp.gmail.com
MAIL_DELIVERY_PORT=587
MAIL_DELIVERY_AUTH=true
MAIL_DELIVERY_STARTTLS=true
MAIL_DELIVERY_SSL=false
MAIL_USERNAME=YOUR_GMAIL_ADDRESS
MAIL_PASSWORD=YOUR_GOOGLE_APP_PASSWORD
MAIL_FROM=YOUR_GMAIL_ADDRESS
APP_BASE_URL=https://YOUR_PUBLIC_APPLICATION_HOST
```

For a local-only test, `APP_BASE_URL=http://localhost:3001` is appropriate, but recipients outside the machine cannot open that link.

Apply with `docker compose -p aetheris-forensic -f docker-compose.yml up -d --no-deps --force-recreate api`. Other shared services and volumes stay untouched. Do not print environment variables containing passwords. Verify `MAIL_HOST`, `MAIL_PORT`, and `MAIL_SMTP_AUTH` inside the API container. Use a fresh invitation, because previously exposed activation tokens should be revoked/rotated. Google App Password requires an eligible Google account with 2-Step Verification, and organization policies may restrict it. Prefer an approved SMTP provider for production.

`MAIL_DELIVERY_*` configures the API container without changing other services' mail transport. The original backend `EmailService` handles STARTTLS and SMTP AUTH on port 587. MailHog remains the default if settings are omitted.
