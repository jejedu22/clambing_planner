import os

os.environ.update(APP_ENV="test", DATABASE_URL="sqlite:///./test.db", SECRET_KEY="test-secret",
                  COOKIE_SECURE="0", SUPERADMIN_EMAIL="root@test.org", SUPERADMIN_PASSWORD="rootpassword123", APP_BASE_URL="https://escalade.test", SMTP_HOST="smtp.test", SMTP_FROM="no-reply@test.org")

import re
import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app, bootstrap_superadmin
from app.models import Club, Outing, Role, Site, User
from app.security import hash_password


@pytest.fixture()
def db():
    from app.routes import account, auth, outings
    auth._failures.clear()
    account.forgot_by_email.clear(); account.forgot_by_ip.clear()
    outings._items_cache.clear()
    from app.services import helloasso
    helloasso._tokens.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    bootstrap_superadmin()
    with SessionLocal() as s:
        yield s


def make_user(db, email, role, club_id=None, password="password12345"):
    u = User(email=email, name=email.split("@")[0], role=role, club_id=club_id,
             password_hash=hash_password(password))
    db.add(u); db.commit()
    return u


@pytest.fixture()
def world(db):
    a, b = Club(name="Club A"), Club(name="Club B")
    db.add_all([a, b]); db.commit()
    site = Site(osm_id="node/1", name="Fontainebleau", lat=48.4, lon=2.6, types="boulder")
    db.add(site); db.commit()
    make_user(db, "admin_a@test.org", Role.CLUB_ADMIN.value, a.id)
    make_user(db, "member_a@test.org", Role.MEMBER.value, a.id)
    make_user(db, "admin_b@test.org", Role.CLUB_ADMIN.value, b.id)
    return dict(a=a, b=b, site=site)


def client_for(email, password="password12345"):
    c = TestClient(app)
    token = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"email": email, "password": password, "csrf": token}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def csrf(c, path="/"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


@pytest.fixture()
def outbox(monkeypatch):
    """Remplace l'appel HTTP vers Mailjet : enregistre les messages au lieu de les envoyer."""
    from app.services import mailer
    sent = []

    def fake_post(url, payload, auth):
        sent.append({"url": url, "auth": auth, "sandbox": payload.get("SandboxMode", False),
                     "messages": payload["Messages"]})
        return 200, {"Messages": [{"Status": "success"} for _ in payload["Messages"]]}

    monkeypatch.setattr(mailer, "_post", fake_post)
    return sent


def recipients(outbox):
    return sorted(m["To"][0]["Email"] for call in outbox for m in call["messages"])


def enable_mailjet(db, club_id):
    from app.models import ClubIntegration
    from app.services import crypto
    integ = db.get(ClubIntegration, club_id) or ClubIntegration(club_id=club_id)
    integ.mj_api_key, integ.mj_api_secret_enc = "abcd1234abcd", crypto.encrypt("topsecretmailjet")
    integ.mj_from_email, integ.mj_from_name = "club@test.org", "Club"
    db.merge(integ); db.commit()


def enable_helloasso(db, club_id, form="adhesion-2026"):
    from app.models import ClubIntegration
    from app.services import crypto
    integ = db.get(ClubIntegration, club_id) or ClubIntegration(club_id=club_id)
    integ.ha_client_id, integ.ha_client_secret_enc = "client-id-1", crypto.encrypt("hasecretvalue")
    integ.ha_org_slug, integ.ha_membership_form = "mon-club", form
    db.merge(integ); db.commit()


@pytest.fixture()
def smtp_outbox(monkeypatch):
    """Remplace la connexion SMTP : enregistre les messages (destinataire, sujet, texte)."""
    from app.services import smtp_mailer
    sent = []

    def fake_deliver(msg):
        sent.append({"to": msg["To"], "subject": msg["Subject"], "from": msg["From"],
                     "text": msg.get_body(("plain",)).get_content(), "html": msg.get_body(("html",)).get_content()})

    monkeypatch.setattr(smtp_mailer, "_deliver", fake_deliver)
    return sent
