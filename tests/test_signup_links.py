import html
import logging
import re
from datetime import timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditLog, SignupLink, User, utcnow
from .conftest import client_for, csrf, enable_mailjet, recipients

FORM = {"first_name": "Lou", "last_name": "Reed", "email": "lou@test.org", "phone": "", "password": "walk-on-the-wild",
        "confirm": "walk-on-the-wild", "consent": "1"}


def new_link(c, **opts):
    r = c.post("/admin/signup-links", data={"csrf": csrf(c, "/admin/signup-links"), "validity": "30", "approval": "1", **opts})
    assert r.status_code == 200, r.text
    url = html.unescape(re.search(r'id="new-link" value="([^"]+)"', r.text).group(1))
    return "/" + url.split("/", 3)[3]                                       # chemin /rejoindre/<jeton>


def join(path, client=None, **over):
    c = client or TestClient(app)
    return c.post(path, data={**FORM, **over, "csrf": csrf(c, path)}, follow_redirects=False)


def test_no_public_signup_without_link(world):
    c = TestClient(app)
    assert c.get("/rejoindre/inventé").status_code == 404 and "Lien non valable" in c.get("/rejoindre/x").text
    assert "Créer mon compte" not in c.get("/").text and "lien d'inscription privé" in c.get("/").text


def test_link_with_approval_flow(world, db, outbox):
    enable_mailjet(db, world["a"].id)
    admin = client_for("admin_a@test.org")
    path = new_link(admin, label="Rentrée")
    token = path.rsplit("/", 1)[1]
    link = db.query(SignupLink).one()
    assert token not in link.token_hash and token not in link.token_enc and len(token) >= 32   # jamais en clair
    page = TestClient(app).get(path).text
    assert "Rejoindre Club A" in page and 'content="no-referrer"' in page
    r = join(path)
    assert r.status_code == 200 and "va valider votre inscription" in r.text
    u = db.query(User).filter_by(email="lou@test.org").one()
    assert (u.club_id, u.role, u.is_active, u.pending) == (world["a"].id, "member", False, True)
    assert recipients(outbox) == ["admin_a@test.org"]                       # l'administrateur est prévenu
    # tant que ce n'est pas validé, pas de connexion
    c = TestClient(app)
    r = c.post("/login", data={"email": "lou@test.org", "password": "walk-on-the-wild", "csrf": csrf(c, "/login")})
    assert r.status_code == 403 and "attend la validation" in r.text
    assert "Inscriptions à valider (1)" in admin.get("/admin/users").text
    admin.post(f"/admin/users/{u.id}/approve", data={"csrf": csrf(admin)})
    db.expire_all(); u = db.get(User, u.id)
    assert u.is_active and not u.pending
    assert "Bienvenue à Club A" in outbox[-1]["messages"][0]["Subject"]
    assert client_for("lou@test.org", "walk-on-the-wild").get("/").status_code == 200
    assert db.get(SignupLink, link.id).uses == 1


def test_link_without_approval_logs_in_directly(world, db):
    path = new_link(client_for("admin_a@test.org"), approval="")
    c = TestClient(app)
    r = join(path, c)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Bienvenue à Club A" in c.get("/").text and "À venir" in c.get("/").text
    assert db.query(User).filter_by(email="lou@test.org").one().is_active is True


def test_reject_pending_signup(world, db):
    admin = client_for("admin_a@test.org"); path = new_link(admin)
    join(path); uid = db.query(User).filter_by(email="lou@test.org").one().id
    admin.post(f"/admin/users/{uid}/reject", data={"csrf": csrf(admin)})
    assert db.query(User).filter_by(email="lou@test.org").count() == 0
    assert db.query(AuditLog).filter_by(event="user.signup_rejected").count() == 1


def test_activating_by_checkbox_also_validates(world, db):
    admin = client_for("admin_a@test.org"); path = new_link(admin)
    join(path); u = db.query(User).filter_by(email="lou@test.org").one()
    admin.post(f"/admin/users/{u.id}", data={"role": "member", "active": "1", "csrf": csrf(admin, "/admin/users")})
    db.expire_all()
    assert db.get(User, u.id).pending is False
    assert client_for("lou@test.org", "walk-on-the-wild").get("/").status_code == 200


def test_revoked_expired_and_full_links(world, db):
    admin = client_for("admin_a@test.org")
    path = new_link(admin, max_uses="1", approval="")
    assert join(path).status_code == 303
    assert join(path, email="second@test.org").status_code == 404           # limite atteinte
    p2 = new_link(admin); l2 = db.query(SignupLink).order_by(SignupLink.id.desc()).first()
    admin.post(f"/admin/signup-links/{l2.id}/revoke", data={"csrf": csrf(admin, "/admin/signup-links")})
    assert TestClient(app).get(p2).status_code == 404 and join(p2, email="t@test.org").status_code == 404
    p3 = new_link(admin); l3 = db.query(SignupLink).order_by(SignupLink.id.desc()).first()
    l3.expires_at = utcnow() - timedelta(minutes=1); db.commit()
    assert TestClient(app).get(p3).status_code == 404
    page = admin.get("/admin/signup-links").text
    assert "Complet" in page and "Révoqué" in page and "Expiré" in page


def test_full_link_is_atomic(world, db):
    """La limite est appliquée par la base : même si l'objet en mémoire paraît encore valable."""
    admin = client_for("admin_a@test.org"); path = new_link(admin, max_uses="1", approval="")
    link = db.query(SignupLink).one(); link.uses = 1; db.commit()             # utilisé entre-temps par quelqu'un d'autre
    assert join(path).status_code == 404 and db.query(User).filter_by(email="lou@test.org").count() == 0


def test_signup_validation_and_protections(world, db):
    path = new_link(client_for("admin_a@test.org"))
    for over, msg in ((dict(consent=""), "accepter"), (dict(confirm="different-pass"), "ne correspondent pas"),
                      (dict(password="court", confirm="court"), "au moins 10"), (dict(email="nope"), "email invalide"),
                      (dict(last_name=""), "prénom et votre nom"), (dict(email="member_a@test.org"), "Un compte existe déjà")):
        r = join(path, **over)
        assert r.status_code == 400 and msg in r.text, over
    r = join(path, website="http://spam")                                    # champ piège
    assert "va valider" in r.text and db.query(User).filter_by(email="lou@test.org").count() == 0
    assert TestClient(app).post(path, data=FORM).status_code == 403            # CSRF
    c = TestClient(app)
    for i in range(5):
        join(path, c, email=f"u{i}@test.org")
    assert join(path, c, email="u9@test.org").status_code == 429              # limite par IP
    assert db.get(SignupLink, 1).uses == 5


def test_token_never_logged(world, caplog):
    caplog.set_level(logging.DEBUG)
    path = new_link(client_for("admin_a@test.org"))
    token = path.rsplit("/", 1)[1]
    caplog.clear(); TestClient(app).get(path); join(path)
    assert token not in " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert any("/rejoindre/[masqué]" in r.getMessage() for r in caplog.records)


def test_permissions_and_isolation(world, db):
    m = client_for("member_a@test.org")
    assert m.get("/admin/signup-links").status_code == 403
    a, b = client_for("admin_a@test.org"), client_for("admin_b@test.org")
    path = new_link(a, label="LIEN-SECRET-A")
    page_b = b.get("/admin/signup-links").text
    assert "LIEN-SECRET-A" not in page_b and path.rsplit("/", 1)[1] not in page_b
    assert "LIEN-SECRET-A" in a.get("/admin/signup-links").text
    lid = db.query(SignupLink).one().id
    assert b.post(f"/admin/signup-links/{lid}/revoke", data={"csrf": csrf(b, "/admin/signup-links")}).status_code == 404
    new_link(a, club_id=str(world["b"].id))                                  # club forgé : lien créé pour le club A
    assert {l.club_id for l in db.query(SignupLink)} == {world["a"].id}
    join(path); uid = db.query(User).filter_by(email="lou@test.org").one().id
    assert b.post(f"/admin/users/{uid}/approve", data={"csrf": csrf(b)}).status_code == 404     # autre club
    assert m.post(f"/admin/users/{uid}/approve", data={"csrf": csrf(m)}).status_code == 403
    root = client_for("root@test.org", "rootpassword123")
    assert "Inscriptions à valider" in root.get("/admin/users").text
    root.post("/admin/signup-links", data={"csrf": csrf(root, "/admin/signup-links"), "club_id": str(world["b"].id), "validity": "7"})
    assert db.query(SignupLink).filter_by(club_id=world["b"].id).count() == 1


def test_bad_link_parameters(world, db):
    a = client_for("admin_a@test.org")
    for opts in ({"validity": "9999"}, {"max_uses": "0"}, {"max_uses": "abc"}, {"label": "x" * 81}):
        r = a.post("/admin/signup-links", data={"csrf": csrf(a, "/admin/signup-links"), "validity": "30", **opts})
        assert r.status_code == 400, opts
    assert db.query(SignupLink).count() == 0
