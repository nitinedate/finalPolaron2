# Polaron V1.29.0: production HTTPS on the IP address or domain from .env

`script_docker\start_docker_prod.cmd` runs the same pipeline as `start_docker_local.cmd`:
- the same cached build;
- the same shared Common Docker;
- the same product stacks;
- the same host Ollama and SuryaOCR checks;
- the same evidence-safe restart.

The difference is that it serves the application over **trusted HTTPS** on the server's public IP address and, when
one is configured, on a domain name. Certificates come from Let's Encrypt and **renew automatically**.

## Configure (root `.env`)
```ini
PUBLIC_HTTPS_IP=122.179.141.248     # static public IPv4 of this server (required)
PUBLIC_HTTPS_DOMAIN=                # optional; empty = HTTPS on the IP only
PUBLIC_HTTPS_EMAIL=you@company.com  # Let's Encrypt account email (asked once if empty)
```
- **No domain:** only `https://<IP>/` is served. No DNS is needed.
- **With a domain:** both `https://<IP>/` and `https://<domain>/` work, each with its own certificate.
  - The domain's public DNS A record must point to `PUBLIC_HTTPS_IP`.
  - Plain HTTP redirects to HTTPS on whichever of the two the browser asked for.
- The router must forward **public TCP 80 and 443** to this Windows PC. Let's Encrypt checks TCP 80 on the first issue
  and on every renewal.
- Run the launcher as Administrator once so it can create the Windows Firewall rules.
- Command-line options (`-PublicIP`, `-Domain`, `-AcmeEmail`) still override `.env` for a single launch.
- While `PUBLIC_HTTPS_IP` is empty, the old `prod` entry in `script_docker\start_docker.settings.json` is used, and
  the launcher prints the `.env` lines to add.
- The launcher reads `.env` but never rewrites it.
- `start_docker_nitin.cmd` is unchanged and keeps its settings-file entry.

This project's `.env` now contains `PUBLIC_HTTPS_IP=122.179.141.248`, an empty `PUBLIC_HTTPS_DOMAIN`, and the
Let's Encrypt email that was already in the settings file. These values are the ones the prod profile used before.

## What changed
1. **Address from `.env`; domain allowed for prod.** Before, prod read only the settings file and refused any
   domain. A domain is validated as a hostname, which also blocks nginx injection, and an IP address is refused as a
   domain.
2. **The HTTPS gateway now matches the local gateway.** It has the same routes and the same readable JSON error when a
   product API is stopped or still starting (`@api_unreachable`, the `$svc_id`/`$svc_label` maps), the same `/health`
   timeouts, and errors logged to the container log.
3. **Certificates are issued and renewed per identity.**
   - The IP and the domain are attempted separately, so one failing never blocks the other.
   - If a newly added domain cannot get its certificate yet (usually DNS not ready), HTTPS starts on the IP only. The
     launcher says why, and the domain is added on a later launch. Before, nginx refused to start.
   - A failed renewal with a stored certificate keeps serving the stored one, as before.
4. **The renewer renews only what the gateway serves.**
   - `certbot-renewer` checks every 6 hours and reloads nginx only after a renewal.
   - It now renews exactly the served certificates (`AETHERIS_CERT_NAMES`). A certificate for an old IP or a removed
     domain no longer fails every 6 hours.
   - IP certificates are Let's Encrypt short-lived certificates (about 6 days); Certbot renews them at half-life.
     Domain certificates last 90 days.
5. **Faster restarts.** When every certificate has more than 36 hours left, a launch needs no HTTP-01 challenge. The
   gateway is recreated with the new frontend and the renewer is restarted, without pausing TCP 80. Before, the
   renewer stopped with the gateway, so almost every launch ran the challenge.
6. **Emailed links.** Activation, password-reset and login emails use `https://<domain, or IP>` while `APP_BASE_URL`
   in `.env` is a localhost address. A custom `APP_BASE_URL` is kept.
7. **Messages.** `start_docker_prod.cmd` explains the `.env` settings, and READY lists the HTTPS address(es).

## Use
1. Edit `PUBLIC_HTTPS_IP` / `PUBLIC_HTTPS_DOMAIN` / `PUBLIC_HTTPS_EMAIL` in `.env`.
2. Make sure public TCP 80 and 443 reach this PC. For a domain, set its A record to the IP.
3. Run `script_docker\start_docker_prod.cmd`, as Administrator the first time.
4. Open the `READY: https://...` address it prints.
5. To check renewals:
   ```
   docker compose --project-directory . --project-name aetheris-gateway -f services/gateway/docker-compose.yml \
     -f deployment/windows-ip-https/docker-compose.gateway-local.yml \
     -f deployment/windows-ip-https/docker-compose.gateway-public-https.yml logs --tail 80 certbot-renewer
   ```

To change the address later, edit `.env` and run the launcher again: a new certificate is issued for a new IP or
domain. To go back to IP only, empty `PUBLIC_HTTPS_DOMAIN`.

## Verification (offline)
- **PowerShell 7.4 parser:** `start_docker.ps1`, `start_docker.lib.ps1` and both check scripts parse cleanly. The
  scripts stay ASCII with CRLF line endings for Windows PowerShell 5.1.
- **`tests/windows_docker_startup_checks.ps1`:** 84 checks pass, 17 of them new. They cover:
  - `.env` precedence, quotes and comments, the last value winning, and the command line overriding `.env`;
  - IP-only mode and domain normalization;
  - LAN-IP and bad-domain rejection;
  - nitin unaffected;
  - `.env` never rewritten;
  - `APP_BASE_URL` rules;
  - the nginx maps.
- **Startup checks run against the original launcher:** the same 3 checks fail before and after this release. They
  look for a Podman stop and port-clearing calls that the launcher no longer contains. There are no new failures.
- **`tests/prod_https_edge_checks.ps1` (new):** 19 checks of the certificate flow, with Docker and ACME mocked. They
  cover:
  - unchanged restart: no challenge, renewer restarted;
  - code change: no challenge;
  - due certificate renewed;
  - domain without a certificate: IP only;
  - failed renewal: stored certificates used;
  - TCP 80 unreachable;
  - first launch without TCP 80: clear error, bootstrap removed;
  - domain removed;
  - `-Recreate`.
- **nginx 1.24, `nginx -t`:** the generated IP-only and IP+domain configurations pass with the real
  `gateway-https-locations.inc` and `gateway-proxy.inc`. Run with test certificates, they showed:
  - HTTP to HTTPS keeps the requested IP or domain;
  - the ACME challenge and `/gateway-health` are served on HTTP;
  - the right certificate is chosen for each name (SNI);
  - a stopped API returns the readable JSON 502.
- **`renew-certificates.sh`** was tested under dash and BusyBox (the Certbot image's shell) with a fake `certbot`:
  only the listed certificates are renewed, invalid names are skipped, a failure is reported per name, and the
  script stops cleanly on TERM.
- **pytest:** `test_prod_https_env_v1_29.py` (7 new), `test_start_docker_local_v1108.py` and
  `test_malware_launcher.py` give 22 passed.

## Not verified here
- A real Let's Encrypt issuance or renewal, router forwarding and Docker Desktop on the server. This happens on the
  first run of `start_docker_prod.cmd`.
- **On the server itself or inside the LAN,** `https://<public IP>/` works only if the router supports NAT loopback
  (hairpin). `http://localhost:3001/` on the server redirects there. With a domain, a local DNS entry can point the
  domain to the LAN address.
- **Client-site imaging (V1.27)** from the HTTPS page calls the examiner PC's local helpers. Current Chrome and Edge
  may ask the examiner to allow the site to access apps on this device; allow it.

## Rollback
Restore the files from `recovery-snapshots/pre-v1.29.0-prod-https-20261115/`. Copy `env.backup` back to `.env`
only if you want to drop the new `PUBLIC_HTTPS_*` lines; the old launcher ignores them.
