"""Safe SMTP endpoint diagnostic. Run INSIDE API container; no credentials sent."""
import os
import smtplib
import ssl
import sys

host = os.getenv("MAIL_HOST", "").strip()
port = int(os.getenv("MAIL_PORT", "587"))
ssl_on = os.getenv("MAIL_SMTP_SSL", "false").lower() in ("true", "1", "yes")
tls_on = os.getenv("MAIL_SMTP_STARTTLS", "true").lower() in ("true", "1", "yes")
if not host or host.lower() in ("mailhog", "localhost", "127.0.0.1"):
    print("ERROR: Configure an external SMTP server; MailHog is capture-only.")
    sys.exit(2)
try:
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL(host, port, timeout=12, context=context) if ssl_on else smtplib.SMTP(host, port, timeout=12)
    try:
        code, _ = client.ehlo()
        print(f"Server: {host}:{port}; initial EHLO={code}; implicit_tls={ssl_on}")
        if tls_on and not ssl_on:
            if not client.has_extn("starttls"):
                raise RuntimeError("STARTTLS not advertised by server")
            client.starttls(context=context)
            code, _ = client.ehlo()
            print(f"After STARTTLS: EHLO={code}")
        mechanisms = client.esmtp_features.get("auth", "")
        print("SMTP AUTH advertised:", bool(mechanisms))
        print("AUTH mechanisms:", " ".join(sorted(mechanisms.split())) or "none")
        print("No password submitted; no invitation sent.")
    finally:
        try:
            client.quit()
        except Exception:
            client.close()
except Exception as error:
    print("SMTP diagnostic failed:", type(error).__name__)
    sys.exit(1)
