# Polaron SMTP AUTH disconnect repair

## Observed problem
`POST /api/tenants/{firm_id}/users/{user_id}/invite` caused an unhandled `MailDeliveryError` (HTTP 500) when the remote SMTP server disconnected during AUTH. This patch returns controlled HTTP 503 `invite_delivery_failed` and rolls back the uncommitted invitation token.

## Root-cause isolation
The traceback alone does not distinguish Gmail rejection, network/proxy interference, a wrong host or port, or a TLS mismatch. The transport now emits safe capability diagnostics (server, port, TLS mode, advertised AUTH mechanisms). It refuses to authenticate to a server that does not advertise SMTP AUTH. Passwords, invitation tokens and message content are not logged.

## Configure
In the existing project `.env` (do not include secrets in support logs):

```
MAIL_HOST=smtp.gmail.com
MAIL_PORT=587
MAIL_USERNAME=<your Gmail mailbox>
MAIL_PASSWORD=<valid Google App Password>
MAIL_FROM=<same Gmail mailbox or permitted alias>
MAIL_SMTP_AUTH=true
MAIL_SMTP_STARTTLS=true
MAIL_SMTP_SSL=false
MAIL_SEND_ENABLED=true
```

Note: SMTP port 465 instead uses SSL=true and STARTTLS=false. SMTP server settings must be adjusted to your actual provider; a Google App Password is available only for eligible accounts.

## Deploy
Extract the patch ZIP over `D:\project1\polaran2`, retaining directory structure. Rebuild the API **image** because Python code changed; simply using `up --force-recreate` with the old image will NOT apply the fix:

```
cd /d D:\project1\polaran2
script_docker\start_docker_local.cmd
```

Or, if using the root Compose file directly:

```
docker compose -p aetheris-forensic -f docker-compose.yml up -d --build --no-deps api
```

Be aware that the project launcher may restart Polaron application containers; stop active forensic jobs first.

## Diagnostics within the API container

```
docker exec aetheris-forensic-api-1 python -m scripts.smtp_diagnose
```

If the container does not package `backend/scripts` as `scripts`, run a one-off Python SMTP EHLO/STARTTLS probe instead, using `docker exec ... python -c` and `smtplib`.

Check the actual running settings, **never print the password**:

```
docker exec aetheris-forensic-api-1 sh -c 'printf "%s %s %s %s\n" "$MAIL_HOST" "$MAIL_PORT" "$MAIL_SMTP_AUTH" "$MAIL_SMTP_STARTTLS"'
```

If Gmail advertises `AUTH PLAIN LOGIN` after STARTTLS but closes on AUTH despite a correct App Password, check firewall/antivirus SMTP inspection, VPN/proxy, Google account policy, and outbound port 587. Do not disable TLS or fall back to MailHog. For enterprise sending consider a transactional SMTP provider.

**Important:** SMTP acceptance is not inbox delivery. Check provider logs, bounces, spam filtering, SPF/DKIM/DMARC, and use a publicly reachable HTTPS `APP_BASE_URL` for remote recipients.

Do not forward invitation tokens or account passwords in logs. Invalidate previously exposed tokens.
