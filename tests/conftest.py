import os

os.environ.update(APP_ENV="test", DATABASE_URL="sqlite:///./test.db", SECRET_KEY="test-secret",
                  COOKIE_SECURE="0", SUPERADMIN_EMAIL="root@test.org", SUPERADMIN_PASSWORD="rootpassword123")

import re
import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app, bootstrap_superadmin
from app.models import Club, Outing, Role, Site, User
from app.security import hash_password


@pytest.fixture()
def db():
    from app.routes import auth
    auth._failures.clear()
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
