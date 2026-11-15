# Windows Docker startup — first-launch configuration fix

Use the launchers from the existing project root. They compile the application
on every launch and retain Docker's dependency cache.

| Launcher | Website | Certificates | Public ports |
| --- | --- | --- | --- |
| `start_docker_local.cmd` | `http://localhost:3000/` and `http://localhost:3001/` | None required for local HTTP | None; gateway binds to loopback |
| `start_docker_nitin.cmd` | `https://future-softtech.co.in/` and `https://122.179.140.167/` by default | Separate trusted domain and IP certificates | TCP 80 and 443 |
| `start_docker_prod.cmd` | `https://<PUBLIC_HTTPS_IP>/` and, when set, `https://<PUBLIC_HTTPS_DOMAIN>/` (root `.env`) | Trusted IP certificate; domain certificate only when a domain is set | TCP 80 and 443 |

The public address is configuration, not automatic detection of your ISP
address. For `start_docker_prod.cmd` set it in the root `.env` (V1.29):

```ini
PUBLIC_HTTPS_IP=122.179.141.248      # static public IPv4 of this server (required)
PUBLIC_HTTPS_DOMAIN=                 # optional; empty = HTTPS on the IP only
PUBLIC_HTTPS_EMAIL=admin@your-company.com   # Let's Encrypt notices; asked once if empty
```

With a domain, both `https://<IP>/` and `https://<domain>/` work, each with its
own trusted certificate; plain HTTP redirects to HTTPS on whichever of the two
was typed. Without a domain only the IP is served. While `PUBLIC_HTTPS_IP` is
empty, the `prod` entry of `script_docker/start_docker.settings.json` is used as
before (the launcher then prints the `.env` lines to add). `start_docker_nitin.cmd`
keeps using its settings-file entry.

## Install and launch

1. Merge the updated project or startup patch into your existing project.
   Preserve `.env`, evidence, backup directories, database volumes and scanner
   tokens. Run this deployment after active extraction/scan jobs finish.
2. Start Docker Desktop in **Linux container mode**. Docker Compose **2.24.4+
   with Buildx/BuildKit** is required. Existing NVIDIA/WSL requirements for the
   product's GPU services still apply.
3. Launching any profile creates a missing root `.env` from `.env.example`,
   with fresh JWT and shared client-token signing secrets. All product stacks
   use this same root file. Existing `.env` values are preserved exactly.
   Missing `script_docker/start_docker.settings.json` is also created from its
   example. SMTP credentials and other host-specific settings can be entered
   in the generated files. No pre-generated signing secret is distributed.
   `tools/host-python-requirements.txt` is now included for the Windows USB kit.
4. For public HTTPS, reserve the Windows PC's LAN address and forward router
   **TCP 80 → Windows TCP 80** and **TCP 443 → Windows TCP 443**. Allow both
   inbound ports in Windows Firewall. Running the launcher as Administrator
   creates the named firewall rules. The ISP must allow inbound public TCP 80
   for certificate validation and renewal.
5. When a domain is used (always for Nitin, optional `PUBLIC_HTTPS_DOMAIN` for
   production), its public DNS A record must point to the static IP. Any AAAA
   record must also reach the challenge-serving host. If the domain certificate
   cannot be issued yet (DNS not ready), production starts HTTPS on the IP only,
   prints the reason, and adds the domain on a later launch.

Run one profile from PowerShell:

```powershell
Set-Location E:\polaron-forensic
.\script_docker\start_docker_local.cmd
```

```powershell
Set-Location E:\polaron-forensic
.\script_docker\start_docker_nitin.cmd
```

```powershell
Set-Location E:\polaron-forensic
.\script_docker\start_docker_prod.cmd
```

On the first public launch without an email (`PUBLIC_HTTPS_EMAIL`, `-AcmeEmail`
or `CERTBOT_EMAIL`), enter the email address for Let's Encrypt; it is saved in
`script_docker/start_docker.settings.json`. The launcher never rewrites `.env`.
Values that come from `.env` are not copied into the settings file.

Arguments override `.env` and the settings file for that launch (they are saved
to the settings file only when `.env` does not set that value):

```powershell
.\script_docker\start_docker_prod.cmd -PublicIP "YOUR_STATIC_PUBLIC_IP" -Domain "your-domain.example" -AcmeEmail "admin@your-company.com"
.\script_docker\start_docker_nitin.cmd -PublicIP "YOUR_STATIC_PUBLIC_IP" -Domain "your-domain.example" -AcmeEmail "admin@your-company.com"
```

`CERTBOT_EMAIL` can supply the email without a prompt. Set
`$env:AETHERIS_NO_PAUSE = "1"` before launching to suppress the final CMD pause
in automated runs. Failures propagate a nonzero exit code.

## What rebuilds and what stays cached

| Component | Every ordinary launch | Dependency changes |
| --- | --- | --- |
| Shared backend | New build ID; Python application compilation; image reused across the four API stacks | OS/Python/Torch layers rebuild when their inputs change |
| Scanner worker | Python application compilation with its own cached dependency stage | Scanner binaries and Python dependencies rebuild when their inputs change |
| Unified, Android and iOS UIs | Three fresh TypeScript/Vite builds | One shared `npm ci` layer from `package-lock.json`; npm downloads are cached |
| Database, Redis, MinIO, nginx and scanner vendor images | Existing images reused; missing images pulled | Update deliberately during maintenance |
| Certificates | Existing ACME lineages reused; Certbot renews only when due | New certificate issued for a new IP/domain or required renewal |

The first build of the updated Dockerfiles installs any missing or newly
changed dependencies. Later launches keep those layers cached. Removing Docker
images/build cache or changing requirements/lockfiles requires rebuilding them.

Mobile Malware Detection (`services/mobile-malware`) starts after the Android and iOS stacks with its own
image (`backend/Dockerfile.malware`), its API on 127.0.0.1:8085 and the gateway route `/api/malware/`;
set `MALWARE_GPU_MODEL_ENABLED=true` to also start its GPU worker. Diagnose it with
`scripts/check-malware-stack.ps1`.

The shared backend is compiled once, then tagged for Android and iOS. The
launcher first verifies compatible backend context, Dockerfile and Torch
channel settings. This prevents four duplicate backend application builds.

For an explicit dependency maintenance rebuild:

```powershell
.\script_docker\start_docker_prod.cmd -RefreshDependencies
```

`-RefreshDependencies` pulls build base images and rebuilds dependency layers.
Ordinary launches never set `--no-cache` or `--pull` for application builds.
The legacy positional `cache` option now means the ordinary cached mode.
`nocache`, `no-cache` and `-NoCache` remain explicit maintenance aliases.

## HTTPS and renewal

The public profiles use pinned **Certbot v5.8.0** in Linux Docker and the
production Let's Encrypt ACME endpoint. IP issuance uses `--ip-address` and
the required `shortlived` profile. IP certificates last about six days, so
the `certbot-renewer` service checks renewal every six hours and reloads nginx
only after successful renewal. It shares nginx's PID namespace and does not
mount the Docker socket.

`certbot-renewer` renews only the certificates the gateway serves
(`AETHERIS_CERT_NAMES`, set by the launcher): a certificate for an old IP or a
removed domain left in the volume is no longer retried every six hours. Domain
certificates last 90 days and also renew automatically. Both the IP and the
domain renew one at a time, so one failing identity never blocks the other.

A launch whose certificates have more than 36 hours left needs no HTTP-01
challenge: the gateway is recreated with the new frontend and the renewer is
restarted, without pausing TCP 80. Emailed activation, reset and login links use
`https://<domain, or IP>` while `APP_BASE_URL` in `.env` is a localhost address.

The first acquisition uses an HTTP-only bootstrap on TCP 80 if the current
gateway cannot already serve the ACME webroot. The bootstrap is removed before
the public gateway starts. Later acquisitions can use the live gateway without
stopping it for the HTTP challenge. A public launch briefly pauses only the
renewal sidecar to avoid concurrent Certbot locks, then recreates it with nginx.

Certificates/accounts persist in `aetheris-gateway_certbot_etc`; challenges
persist in `aetheris-gateway_certbot_www`. Existing self-signed PEM files are
preserved, but the new gateway uses separate ACME-managed certificate names.
No self-signed fallback is generated. Startup verifies certificate SANs,
private-key pairing, expiry and the HTTPS trust chain/hostname. The final trust
probe runs in Linux Docker while connecting to the Windows host, so it uses
the public IP/domain identity without disabling TLS verification. It also verifies
the gateway's proxied backend health endpoint. Windows overrides keep Docker
Desktop's native `host.docker.internal` routing.

Public HTTP redirects to HTTPS on the IP or domain that was requested while
retaining the HTTP-01 challenge location. The HTTPS gateway uses the same routes
and readable "API not reachable" errors as the local gateway. Infrastructure/API ports remain loopback-bound.
For public profiles, compact Android/iOS UIs also bind to loopback; users
access mobile pages through the unified HTTPS website.

Containers/images for other projects and unrelated Windows port listeners are
left running. A TCP 80/443 conflict fails with the owner name. Reconfigure that
owner before retrying. The scripts preserve data volumes and do not prune cache.
The existing `-AutoRecoverEngine` option is opt-in for a stuck Docker Desktop
engine; ordinary launches do not restart the engine automatically.

## Diagnostics

The volume-startup follow-up is described in `DOCKER-VOLUME-PROBE-FIX.md`.
Optional absent legacy volumes are skipped through the native-command capture
helper. First-use PostgreSQL/pgAdmin volumes are created after a confirmed
missing-volume response. Other Docker errors remain failures. Legacy copies are
staged before being promoted and marked ready, allowing a failed copy to retry.

Prepare all required configuration files without requiring Docker or starting
any containers:

```powershell
Set-Location E:\polaron-forensic
.\script_docker\start_docker_local.cmd -PrepareOnly
```

Validate the six product/shared/gateway Compose configurations and their paths
without builds, containers, public certificate requests or email prompts:

```powershell
.\script_docker\start_docker_local.cmd -ValidateOnly
.\script_docker\start_docker_nitin.cmd -ValidateOnly
.\script_docker\start_docker_prod.cmd -ValidateOnly
```

`script_docker/start_docker.required-files.json` lists 56 common startup/build
files and five additional HTTPS files. Missing templates, source files or
nginx/SQL bind-mount files are reported by name before building. Restore them
from the complete updated project. During real startup, missing Compose-resolved
runtime directories for evidence, scratch, backup, MinIO, Ollama and Hugging
Face are created. Missing static configuration files are never substituted
with empty directories. Existing evidence and database volumes are retained.

An existing empty `.env` or invalid settings JSON stops with an actionable
message rather than overwriting that file. Public forwarding/DNS hints are
only displayed for public deployment failures.

Certificate renewal logs, using the same gateway configuration as the launcher:

```powershell
docker compose --project-directory . --project-name aetheris-gateway -f services/gateway/docker-compose.yml -f deployment/windows-ip-https/docker-compose.gateway-local.yml -f deployment/windows-ip-https/docker-compose.gateway-public-https.yml logs --tail 80 certbot-renewer
```

Do not append `-k` to certificate/trust checks. If HTTP-01 fails, check router
forwarding, Windows Firewall, DNS for Nitin, and ISP inbound TCP 80 first.
Local route validation cannot prove that the ISP/router exposes the public port;
Let's Encrypt validates that externally during actual issuance.

## Validation for this release

- PowerShell 7.4.13 on Linux parser checks, 32 new first-launch file checks and
  31 existing offline startup behavior checks passed.
- Six complete first-launch scenarios passed with mocked Docker operations: local,
  production, Nitin, certificate reuse, cached mode and dependency refresh.
- Three clean installations passed prepare-only and validate-only checks:
  six configurations per profile, 18 checks using the official Compose 2.39.4
  CLI. These checks required no root `.env` or host settings at installation.
  Existing settings remained unchanged and validation created no runtime folders.
- 21 certificate/TLS tests passed, including IP/domain SANs, wrong keys,
  wrong identities, staging/self-signed/expired certificates and trust failures.
- Earlier release-8 checks: both generated nginx configurations passed nginx 1.27.5 checks. Isolated
  nginx tests served HTTP-01, proxied backend health, and validated three IP/domain TLS endpoints using
  a local test CA. These checks did not request public certificates.
- Backend, frontend and scanner application source and Docker dependency stages
  are unchanged by this file-initialization update. The prior build and forensic
  verification records remain historical; the new checks cover startup changes.

Native Windows Docker Desktop image builds, GPU services, public certificate
issuance and full container startup must still be verified on the target host.

Live Windows Docker Desktop builds/cache-hit measurements, public ACME issuance,
router reachability and a real six-hour renewal cycle remain deployment checks.
The downloaded PowerShell runtime verifies syntax/behavior on Linux; Windows
PowerShell 5.1 and Windows-specific firewall/engine calls need the actual host.

Sources checked on 6 October 2026:

- [Docker build cache invalidation](https://docs.docker.com/build/cache/invalidation/)
- [Let's Encrypt IP certificates in Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)
- [Certbot user guide](https://certbot.eff.org/docs/using.html)

- [Docker Desktop host networking](https://docs.docker.com/desktop/features/networking/networking-how-tos/)
