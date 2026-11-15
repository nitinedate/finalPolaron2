#!/bin/sh
# Docker Compose joins this service to the gateway PID namespace, whose PID 1
# is the nginx master (docker-entrypoint.sh execs nginx).
# Existing ACME lineages and webroot paths are persisted in the certificate volume.
# AETHERIS_CERT_NAMES is set by start_docker.ps1 to the certificates the gateway
# serves (the IP and, when configured, the domain). Only those are renewed, so a
# certificate for a previous IP or domain left in the volume cannot fail every
# 6 hours. When it is empty, every stored certificate is renewed.
child_pid=""
trap 'if [ -n "$child_pid" ]; then kill -TERM "$child_pid" 2>/dev/null || true; wait "$child_pid" 2>/dev/null || true; fi; exit 0' TERM INT

renew() {
    certbot renew --non-interactive --quiet --no-random-sleep-on-renew \
        --deploy-hook 'kill -HUP 1' "$@" &
    child_pid=$!
    wait "$child_pid"
    status=$?
    child_pid=""
    return "$status"
}

while :; do
    failed=""
    if [ -n "${AETHERIS_CERT_NAMES:-}" ]; then
        for name in $AETHERIS_CERT_NAMES; do
            case "$name" in
                *[!a-z0-9.-]*) echo "Skipping invalid certificate name: $name" >&2; continue ;;
            esac
            renew --cert-name "$name" || failed="$failed $name"
        done
    else
        renew || failed=" all"
    fi
    if [ -z "$failed" ]; then
        echo "Certificate renewal check completed. Nginx reloads only after a renewal."
    else
        echo "Certificate renewal failed for:$failed; existing certificates remain in use. Check public TCP 80 and certbot-renewer logs." >&2
    fi
    child_pid=""
    sleep 21600 &
    child_pid=$!
    wait "$child_pid"
    child_pid=""
done
