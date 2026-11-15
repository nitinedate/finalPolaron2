# Polaron V1.30.0: production HTTPS through the Caddy already running on the server

## The error
`start_docker_prod.cmd` stopped with `TCP 80 is held by caddy (PID 61940)`. Polaron's own HTTPS gateway needs TCP
80/443, but Caddy already owns them on this server. The launcher never stops another program to take its ports.

## What changes
With `PUBLIC_HTTPS_PROXY=caddy` in `.env` (now set in this project's `.env`; the default `auto` picks Caddy
whenever a Caddy process holds TCP 80/443), Polaron becomes part of Caddy:
- **Caddy serves HTTPS.** It serves `https://<PUBLIC_HTTPS_IP>/` and, when set, `https://<PUBLIC_HTTPS_DOMAIN>/`,
  redirects HTTP to HTTPS, and obtains and renews the certificates.
  - **IP:** a Let's Encrypt short-lived certificate (`issuer acme ... { profile shortlived }`) over HTTP-01 on
    Caddy's TCP 80. TLS-ALPN is turned off for the IP site to avoid the TLS-ALPN problem with IP certificates
    reported for older Caddy builds.
  - **Domain:** Caddy's default certificate.
- **Polaron's gateway runs on loopback only** (`127.0.0.1:3000/3001`), with the same routes and readable errors as
  `start_docker_local`, and Caddy proxies every request to it (streamed). Polaron's Certbot containers are removed.
- **How the launcher changes Caddy:**
  1. finds the running Caddy and its Caddyfile, from the Caddy process (`--config`) or from `CADDY_EXE` /
     `CADDY_CONFIG` in `.env`;
  2. writes `polaron.caddy` next to the Caddyfile;
  3. appends one line, `import polaron*.caddy`, to the Caddyfile and keeps a backup next to it;
  4. runs `caddy validate`, then `caddy reload`. Caddy's other sites keep running.

  This happens **before** any Polaron container is stopped. If Caddy rejects the config or the reload fails,
  both files are restored, Caddy keeps its current config, and Polaron keeps running.
- **Refused setups (nothing changed):**
  - Caddy older than 2.10, which cannot request IP certificates: update it with `caddy upgrade`;
  - a JSON Caddy config;
  - a Caddyfile that cannot be identified: set `CADDY_CONFIG`.
- **Manual mode:** `CADDY_MANAGE_CONFIG=false` only writes `deployment\caddy\polaron.caddy` and leaves Caddy to
  the administrator.
- **Builtin mode:** `PUBLIC_HTTPS_PROXY=builtin` keeps the V1.29 behaviour (Polaron nginx + Certbot on 80/443).

## What you do
1. Make sure the Caddy service is running. The launcher must run **as Administrator** so it can see Caddy's
   command line. Otherwise set `CADDY_CONFIG=C:\path\to\Caddyfile` (and `CADDY_EXE`) in `.env`.
2. Keep public TCP 80 and 443 forwarded to this server (they already reach Caddy).
3. Run `script_docker\start_docker_prod.cmd`. Look for `Caddy now serves https://122.179.141.248/ ...`, then
   `READY: https://122.179.141.248/`.
4. Open `https://122.179.141.248/` from outside the LAN, for example a phone on mobile data. Caddy's log shows
   the certificate being obtained.

## Verification
- **Real Caddy (Ubuntu's 2.6.2) with an existing site of its own:** 16 checks pass.
  - Polaron's IP and domain sites go live through `caddy reload`, and the existing site still answers.
  - They proxy to `127.0.0.1:3001` with `flush_interval -1`, and HTTP redirects to HTTPS for the IP and the domain.
  - The Caddyfile keeps its original text plus one import, and a second launch changes nothing.
  - A conflicting site for the same IP is refused with Caddy's reason ("ambiguous site definition"); the files are
    restored byte for byte and the running config is unchanged.
  - A refused reload is also restored.
  - The import glob stays valid when `polaron.caddy` is deleted.
  - Only two parts needed Caddy 2.10+ and were adapted for this test: the version gate and the
    `profile shortlived` line.
- **`tests/caddy_integration_checks.ps1`:** 50 offline checks, with Caddy and Docker mocked. They cover:
  - mode selection;
  - reading `--config` from Windows command lines;
  - the site file, including that nothing can be injected from `.env`;
  - the import line (once, CRLF and BOM kept);
  - every refusal;
  - restore on validate/reload failure;
  - manual mode;
  - the loopback gateway;
  - that Caddy is changed before containers stop.
- **Existing suites:** dependency reuse 37, HTTPS edge 19, startup 84 (same 3 known pre-existing failures),
  pytest 36.

Not verified here: Caddy on your Windows server, Let's Encrypt actually issuing the IP certificate through it, and
`profile shortlived` on Caddy 2.10+ (the setting Caddy documents for IP certificates).

## Rollback
- Set `PUBLIC_HTTPS_PROXY=builtin`. Only do this if Caddy no longer holds TCP 80/443.
- To remove Polaron from Caddy, delete `polaron.caddy` next to the Caddyfile and run `caddy reload`.
- Polaron files: `recovery-snapshots/pre-v1.30.0-caddy-20261115/`.
