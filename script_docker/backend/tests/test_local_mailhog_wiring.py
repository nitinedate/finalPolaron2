from pathlib import Path
import ast
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_local_api_mailhog_override():
    config = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    env = config['services']['api']['environment']
    assert env['MAIL_HOST'] == 'mailhog'
    assert str(env['MAIL_PORT']) == '1025'
    for name in ('MAIL_SMTP_AUTH','MAIL_SMTP_STARTTLS','MAIL_SMTP_SSL'):
        assert str(env[name]).lower() == 'false'


def test_production_has_no_mailhog_forced_override():
    text = (ROOT / 'docker-compose.https.yml').read_text()
    assert 'MAIL_HOST: mailhog' not in text


def test_router_handles_mail_failure_and_rolls_back():
    tree = ast.parse((ROOT / 'backend/app/routers/tenants.py').read_text())
    route = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'create_tenant')
    handlers = [n for n in ast.walk(route) if isinstance(n, ast.ExceptHandler)]
    handler = next(n for n in handlers if isinstance(n.type, ast.Name) and n.type.id == 'MailDeliveryError')
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'rollback' for n in ast.walk(handler))
    assert any(isinstance(n, ast.Constant) and n.value == 503 for n in ast.walk(handler))


def test_mail_service_is_syntax_valid():
    ast.parse((ROOT / 'backend/app/services/email_service.py').read_text())


def test_mailhog_sends_without_login_or_tls(monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.services.email_service import EmailService
    from types import SimpleNamespace
    calls = []
    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append(('connect', host, port))
        def ehlo(self): pass
        def login(self, *a, **kw):
            raise AssertionError('MailHog must not receive AUTH')
        def starttls(self, *a, **kw):
            raise AssertionError('MailHog must not receive STARTTLS')
        def send_message(self, msg):
            calls.append(('send', msg['To']))
            return {}
        def quit(self): pass
    monkeypatch.setattr('smtplib.SMTP', SMTP)
    config = SimpleNamespace(
        mail_send_enabled=True, mail_smtp_ssl=False,
        mail_smtp_starttls=False, mail_smtp_auth=False,
        mail_host='mailhog', mail_port=1025, mail_username='',
        mail_password='', mail_from='noreply@forensic.local',
    )
    assert EmailService(config).send('admin@company.com', 'Invitation', 'activate') is True
    assert calls == [('connect', 'mailhog', 1025), ('send', 'admin@company.com')]


def test_mailhog_auth_misconfiguration_fails_explicitly():
    import sys
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.services.email_service import EmailService, MailDeliveryError
    from types import SimpleNamespace
    from pytest import raises
    config = SimpleNamespace(mail_send_enabled=True, mail_host='mailhog', mail_port=1025,
        mail_smtp_auth=True, mail_smtp_starttls=False, mail_smtp_ssl=False,
        mail_username='x', mail_password='y')
    with raises(MailDeliveryError, match='MailHog requires'):
        EmailService(config).send('admin@company.com', 'Invitation', 'activate')
