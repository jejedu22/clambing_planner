import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import audit as audit_mod
from app import logging_setup
from app.config import settings
from app.main import app
from app.models import AuditLog
from app.services import mailer, smtp_mailer
from .conftest import client_for, csrf, make_user
from app.models import Role

MAIL = mailer.Mail("dest@test.org", "Dest", "Sujet", "Bonjour texte", "<p>Bonjour html</p>")


# --- SMTP --------------------------------------------------------------------

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context, self.calls, self.fail = host, port, context, [], False
        FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, context=None): self.calls.append(("starttls", context is not None))
    def login(self, u, p): self.calls.append(("login", u, p))

    def send_message(self, msg):
        if self.fail:
            raise smtplib.SMTPRecipientsRefused({})
        self.calls.append(("send", msg["To"], msg["Subject"], msg.get_body(("plain",)).get_content().strip()))


@pytest.fixture()
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtp_mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_smtp_starttls_with_auth(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "smtp_user", "user"); monkeypatch.setattr(settings, "smtp_password", "pw")
    assert smtp_mailer.send(MAIL) == (True, None)
    calls = fake_smtp.instances[0].calls
    assert calls[0] == ("starttls", True) and calls[1] == ("login", "user", "pw")      # TLS avant les identifiants
    assert calls[2][:3] == ("send", "Dest <dest@test.org>", "Sujet") and calls[2][3] == "Bonjour texte"


def test_smtp_ssl_and_none_modes(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "smtp_security", "ssl"); monkeypatch.setattr(settings, "smtp_port", 465)
    smtp_mailer.send(MAIL)
    assert fake_smtp.instances[0].port == 465 and fake_smtp.instances[0].context is not None
    assert not any(c[0] == "starttls" for c in fake_smtp.instances[0].calls)
    monkeypatch.setattr(settings, "smtp_security", "none")
    smtp_mailer.send(MAIL)
    assert [c[0] for c in fake_smtp.instances[1].calls] == ["send"]                     # sans auth : pas de login


def test_smtp_failures_are_reported_not_raised(fake_smtp, monkeypatch):
    fake_smtp.fail = False
    monkeypatch.setattr(smtp_mailer.smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    ok, err = smtp_mailer.send(MAIL)
    assert not ok and "OSError" in err and "down" not in err                            # pas de détails internes
    monkeypatch.setattr(settings, "smtp_host", "")
    assert smtp_mailer.send(MAIL)[0] is False and not smtp_mailer.configured()


def test_smtp_rejects_header_injection_and_bad_recipient(fake_smtp):
    bad = mailer.Mail("dest@test.org", "X\r\nBcc: evil@x.org", "Sujet\r\nBcc: evil@x.org", "t", "<p>t</p>")
    assert smtp_mailer.send(bad)[0] is True
    sent = fake_smtp.instances[0].calls[-1]
    assert "\n" not in sent[1] and "\n" not in sent[2]
    assert smtp_mailer.send(mailer.Mail("pas-un-email", "X", "S", "t", "t"))[0] is False
    assert smtp_mailer.send(mailer.Mail("a@b.org\nBcc: evil@x.org", "X", "S", "t", "t"))[0] is False


# --- journaux --------------------------------------------------------------------

def test_masking_helpers():
    assert logging_setup.mask_email("alice@example.org") == "a***@example.org" and logging_setup.mask_email("x") == "?"
    assert logging_setup.safe_path("/reset/SECRET-TOKEN_123") == "/reset/[masqué]"
    assert logging_setup.safe_path("/outings/3") == "/outings/3"


def test_request_log_has_id_and_never_leaks_token(world, caplog):
    caplog.set_level(logging.DEBUG, logger="app.request")
    c = TestClient(app)
    r1 = c.get("/reset/SUPERSECRETTOKEN?token=ANOTHERSECRET")
    r2 = c.get("/login")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"] and len(r1.headers["x-request-id"]) == 12
    text = " ".join(rec.getMessage() + str(rec.__dict__) for rec in caplog.records)
    assert "SUPERSECRETTOKEN" not in text and "ANOTHERSECRET" not in text
    rec = next(r for r in caplog.records if "/reset/[masqué]" in r.getMessage())
    assert rec.status == 400 and rec.levelno == logging.WARNING and rec.ms >= 0
    assert next(r for r in caplog.records if "/login" in r.getMessage()).levelno == logging.INFO


def test_request_log_records_user_and_quiet_paths(world, caplog):
    caplog.set_level(logging.DEBUG, logger="app.request")
    c = client_for("member_a@test.org")
    caplog.clear(); c.get("/outings"); c.get("/healthz")
    outing = next(r for r in caplog.records if "/outings" in r.getMessage())
    assert outing.user is not None
    assert next(r for r in caplog.records if "/healthz" in r.getMessage()).levelno == logging.DEBUG   # pas de bruit


def test_unhandled_error_is_logged(world, caplog):
    caplog.set_level(logging.ERROR, logger="app.request")

    @app.get("/boom-test")
    def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        TestClient(app).get("/boom-test")
    assert any("Erreur non gérée" in r.getMessage() and r.exc_info for r in caplog.records)


def test_json_formatter_and_file_rotation(tmp_path, monkeypatch):
    log_file = tmp_path / "logs" / "app.log"
    monkeypatch.setattr(settings, "log_format", "json"); monkeypatch.setattr(settings, "log_file", str(log_file))
    monkeypatch.setattr(settings, "log_max_mb", 1); monkeypatch.setattr(settings, "log_backups", 2)
    root = logging.getLogger(); saved = list(root.handlers)
    try:
        logging_setup.setup_logging()
        logging_setup.request_id_var.set("abc123")
        logging.getLogger("test").warning("événement é", extra={"club": 7})
        for h in root.handlers: h.flush()
        line = json.loads(log_file.read_text().splitlines()[-1])
        assert line["msg"] == "événement é" and line["request_id"] == "abc123" and line["club"] == 7 and line["level"] == "WARNING"
        handler = next(h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler))
        assert handler.maxBytes == 1024 * 1024 and handler.backupCount == 2
        # fichier impossible : on retombe sur la sortie standard, sans planter
        monkeypatch.setattr(settings, "log_file", "/proc/forbidden/app.log")
        logging_setup.setup_logging()
        assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
    finally:
        for h in list(root.handlers): root.removeHandler(h)
        for h in saved: root.addHandler(h)


# --- audit -------------------------------------------------------------------------

def events(db):
    db.expire_all()
    return [(e.event, e.target, e.detail) for e in db.query(AuditLog).order_by(AuditLog.id)]


def test_audit_login_and_password_events(world, db):
    c = TestClient(app)
    c.post("/login", data={"email": "member_a@test.org", "password": "wrong-password", "csrf": csrf(c, "/login")})
    client_for("member_a@test.org")
    ev = events(db)
    assert ("login.failure", "m***@test.org", None) in ev
    assert any(e[0] == "login.success" for e in ev)
    flat = json.dumps(ev)
    assert "member_a@test.org" not in flat and "wrong-password" not in flat      # e-mail masqué, jamais de mot de passe


def test_audit_admin_actions_and_secrets(world, db):
    admin = client_for("admin_a@test.org")
    admin.post("/admin/users", data={"name": "Neo", "email": "neo@test.org", "role": "member", "csrf": csrf(admin, "/admin/users")})
    admin.post("/admin/integrations/mailjet", data={"api_key": "abcd1234abcd", "api_secret": "supersecretvalue",
               "from_email": "club@test.org", "csrf": csrf(admin, "/admin/integrations")})
    ev = events(db); flat = json.dumps(ev)
    assert any(e[0] == "user.created" and "role=member" in e[2] for e in ev)
    assert any(e[0] == "integration.mailjet_saved" for e in ev)
    assert "supersecretvalue" not in flat and "abcd1234abcd" not in flat
    row = db.query(AuditLog).filter_by(event="user.created").one()
    assert row.club_id == world["a"].id and row.ip and "admin_a@test.org" not in row.actor_label and "a***@test.org" in row.actor_label    # IP tracée, e-mail masqué


def test_audit_never_breaks_the_action(world, db, monkeypatch):
    monkeypatch.setattr(audit_mod, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    audit_mod.audit("test.event")        # ne lève pas


def test_audit_purge(world, db):
    old = AuditLog(event="old", at=datetime.now(timezone.utc) - timedelta(days=400))
    new = AuditLog(event="new"); db.add_all([old, new]); db.commit()
    assert audit_mod.purge(180) == 1 and [e.event for e in db.query(AuditLog)] == ["new"]
    assert audit_mod.purge(0) == 0


def test_logs_page_permissions_and_club_scoping(world, db):
    assert client_for("member_a@test.org").get("/admin/logs").status_code == 403
    db.add_all([AuditLog(event="mine.event", club_id=world["a"].id), AuditLog(event="other.event", club_id=world["b"].id),
                AuditLog(event="global.event", club_id=None)]); db.commit()
    a = client_for("admin_a@test.org").get("/admin/logs").text
    assert "mine.event" in a and "other.event" not in a and "global.event" not in a
    # un administrateur ne peut pas élargir son périmètre avec ?club=
    assert "other.event" not in client_for("admin_a@test.org").get(f"/admin/logs?club={world['b'].id}").text
    root = client_for("root@test.org", "rootpassword123")
    full = root.get("/admin/logs").text
    assert all(x in full for x in ("mine.event", "other.event", "global.event"))
    only_b = root.get(f"/admin/logs?club={world['b'].id}").text
    assert "other.event" in only_b and "mine.event" not in only_b
    assert "mine.event" in root.get("/admin/logs?event=mine").text and "other.event" not in root.get("/admin/logs?event=mine").text


def test_uvicorn_access_log_is_disabled():
    """Il contiendrait les jetons /reset/<jeton> en clair."""
    logging_setup.setup_logging()
    access = logging.getLogger("uvicorn.access")
    assert access.disabled and not access.propagate and not access.handlers
