# Routes of every HTTPS server in the generated nginx.gateway.https.conf.
# Kept in step with the local gateway (frontend/nginx-gateway.conf): same product routing and the
# same readable JSON 502 when a product API is stopped or still starting. $hdr_upstream, $svc_id
# and $svc_label are maps defined by the generated configuration.
location ^~ /.well-known/acme-challenge/ {
    root /var/www/certbot;
    default_type text/plain;
    try_files $uri =404;
}

location = /health {
    set $api_upstream http://vuln-api:8080;
    proxy_pass $api_upstream;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_connect_timeout 5s;
    proxy_read_timeout 10s;
}

# Product selection for acquisition is header driven. Never hard-wire to Android.
location /api/acquisition/ {
    set $api_upstream $hdr_upstream;
    include /etc/nginx/conf.d/gateway-proxy.inc;
}

# Mobile Malware Detection: its own product API, selected by path (never by job pinning).
location /api/malware/ {
    set $api_upstream http://malware-api:8080;
    include /etc/nginx/conf.d/gateway-proxy.inc;
    error_page 502 = @malware_unreachable;
}

location /api/vuln/ { set $api_upstream http://vuln-api:8080; include /etc/nginx/conf.d/gateway-proxy.inc; error_page 502 = @api_unreachable; }
location /api/scanners { set $api_upstream http://vuln-api:8080; include /etc/nginx/conf.d/gateway-proxy.inc; error_page 502 = @api_unreachable; }
location /api/scanner-agent/ { set $api_upstream http://vuln-api:8080; include /etc/nginx/conf.d/gateway-proxy.inc; error_page 502 = @api_unreachable; }
location /api/scan-jobs { set $api_upstream http://vuln-api:8080; include /etc/nginx/conf.d/gateway-proxy.inc; error_page 502 = @api_unreachable; }
location /api/scan-policies { set $api_upstream http://vuln-api:8080; include /etc/nginx/conf.d/gateway-proxy.inc; error_page 502 = @api_unreachable; }
location /api/assets { set $api_upstream http://vuln-api:8080; include /etc/nginx/conf.d/gateway-proxy.inc; error_page 502 = @api_unreachable; }
location /api/remediation-tasks { set $api_upstream http://vuln-api:8080; include /etc/nginx/conf.d/gateway-proxy.inc; error_page 502 = @api_unreachable; }

location /api/ {
    set $api_upstream $hdr_upstream;
    include /etc/nginx/conf.d/gateway-proxy.inc;
    error_page 502 = @api_unreachable;
}

# nginx could not reach the product API (stopped, starting, or crashed). Still 502, so the UI's short
# wait-and-retry applies, but with a JSON reason the UI shows instead of a bare "Bad Gateway".
location @malware_unreachable {
    default_type application/json;
    add_header Cache-Control "no-store" always;
    return 502 '{"error":{"code":"service_unreachable","service":"mobile-malware","message":"Malware Detection is not reachable: the malware-api container is stopped, still starting, or failed to start. Run scripts/check-malware-stack.ps1 to see why, or start it with scripts/start-stack.ps1 -Service mobile-malware."}}';
}
location @api_unreachable {
    default_type application/json;
    add_header Cache-Control "no-store" always;
    return 502 '{"error":{"code":"service_unreachable","service":"$svc_id","message":"The $svc_label API is not reachable: its container is stopped, still starting, or failed to start. Start it with scripts/start-stack.ps1 -Service $svc_id and check docker compose ps."}}';
}

location /assets/ {
    expires 1y;
    add_header Cache-Control "public, immutable";
    try_files $uri =404;
}

location / {
    add_header Cache-Control "no-store, no-cache, must-revalidate";
    expires -1;
    try_files $uri $uri/ /index.html;
}
