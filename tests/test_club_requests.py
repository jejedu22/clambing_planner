import re

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditLog, Club, ClubRequest, Role, User
from .conftest import client_for, csrf, make_user

GOOD = {"club_name": "Les Grimpeurs du Vexin", "city": "Pontoise", "first_name": "Inès", "last_name": "Morel",
        "email": "ines@vexin.test", "phone": "06 11 22 33 44", "members": "45", "message": "Bonjour !", "consent": "1"}


def submit(c=None, **over):
    c = c or TestClient(app)
    return c.post("/creer-un-club", data={**GOOD, **over, "csrf": csrf(c, "/creer-un-club")})


def test_landing_for_visitors_agenda_for_members(world, db):
    page = TestClient(app).get("/")
    assert page.status_code == 200 and "Créer mon club" in page.text and "Se connecter" in page.text
    assert "Fontainebleau" not in page.text                                   # aucune donnée de club
    assert "À venir" in client_for("member_a@test.org").get("/").text
    u = db.query(User).filter_by(email="member_a@test.org").one(); u.must_change_password = True; db.commit()
    assert client_for("member_a@test.org").get("/", follow_redirects=False).headers["location"] == "/password"
    assert TestClient(app).get("/outings", follow_redirects=False).headers["location"] == "/login"   # le reste reste privé


def test_request_is_stored_and_super_admins_notified(world, db, smtp_outbox):
    r = submit()
    assert r.status_code == 200 and "Demande envoyée" in r.text
    req = db.query(ClubRequest).one()
    assert (req.club_name, req.email, req.phone, req.members, req.status) == ("Les Grimpeurs du Vexin", "ines@vexin.test", "+33611223344", 45, "pending")
    assert [m["to"] for m in smtp_outbox] == ["Super administrateur <root@test.org>"]
    assert "Les Grimpeurs du Vexin" in smtp_outbox[0]["text"] and "ines@vexin.test" not in smtp_outbox[0]["text"]
    assert f"/admin/requests/{req.id}" in smtp_outbox[0]["text"]
    assert db.query(AuditLog).filter_by(event="club_request.created").count() == 1


def test_request_validation(world, db):
    for over, msg in ((dict(consent=""), "accepter le traitement"), (dict(email="pas-un-email"), "email invalide"),
                      (dict(phone="123"), "téléphone"), (dict(club_name="x"), "nom du club"), (dict(city=""), "ville"),
                      (dict(first_name=""), "prénom et votre nom"), (dict(members="-3"), "Effectif"), (dict(members="abc"), "Effectif"),
                      (dict(message="x" * 2001), "trop long")):
        r = submit(**over)
        assert r.status_code == 400 and msg in r.text, over
        assert 'value="Les Grimpeurs du Vexin"' in r.text or over.get("club_name")      # saisie conservée
    assert db.query(ClubRequest).count() == 0


def test_request_protections(world, db, smtp_outbox):
    r = submit(website="http://spam.example")                                # champ piège rempli
    assert "Demande envoyée" in r.text and db.query(ClubRequest).count() == 0 and smtp_outbox == []
    assert TestClient(app).post("/creer-un-club", data=GOOD).status_code == 403    # CSRF exigé
    assert submit(club_name="club a").status_code == 400                     # nom déjà pris (casse ignorée)
    assert submit().status_code == 200
    r = submit(club_name="Autre nom")                                         # même email, demande en cours
    assert r.status_code == 400 and "déjà en cours" in r.text
    c = TestClient(app)
    for i in range(2):
        assert submit(c, email=f"x{i}@t.test", club_name=f"Club {i}").status_code == 200
    r = submit(c, email="x9@t.test", club_name="Club 9")                      # 4e demande de la même IP en 1 h
    assert r.status_code == 429 and db.query(ClubRequest).count() == 3


def test_request_pages_are_super_admin_only(world, db):
    submit(); rid = db.query(ClubRequest).one().id
    assert TestClient(app).get("/admin/requests", follow_redirects=False).status_code == 303
    for email in ("admin_a@test.org", "member_a@test.org"):
        c = client_for(email)
        assert c.get("/admin/requests").status_code == 403 and c.get(f"/admin/requests/{rid}").status_code == 403
        assert c.post(f"/admin/requests/{rid}/approve", data={"csrf": csrf(c)}).status_code == 403
    root = client_for("root@test.org", "rootpassword123")
    assert "Les Grimpeurs du Vexin" in root.get("/admin/requests").text and "En attente (1)" in root.get("/admin/requests").text
    assert "1 en attente" in root.get("/club").text
    assert root.get("/admin/requests/999").status_code == 404


def test_approve_creates_club_and_admin_with_invitation(world, db, smtp_outbox):
    submit(); rid = db.query(ClubRequest).one().id; smtp_outbox.clear()
    root = client_for("root@test.org", "rootpassword123")
    r = root.post(f"/admin/requests/{rid}/approve", data={"club_name": "Grimpeurs du Vexin", "csrf": csrf(root, f"/admin/requests/{rid}")})
    assert r.status_code == 200 and "créé" in r.text and "lien pour activer" in r.text
    club = db.query(Club).filter_by(name="Grimpeurs du Vexin").one()
    admin = db.query(User).filter_by(email="ines@vexin.test").one()
    assert (admin.role, admin.club_id, admin.first_name, admin.phone, admin.must_change_password) == \
        (Role.CLUB_ADMIN.value, club.id, "Inès", "+33611223344", False)
    db.expire_all(); req = db.get(ClubRequest, rid)
    assert req.status == "approved" and req.club_id == club.id and "Grimpeurs du Vexin" in req.decision_note
    link = re.search(r"https://escalade\.test(/reset/\S+)", smtp_outbox[-1]["text"]).group(1)
    c = TestClient(app)
    c.post(link, data={"new": "vexin-password-1", "confirm": "vexin-password-1", "csrf": csrf(c, link)})
    me = client_for("ines@vexin.test", "vexin-password-1")
    assert "Administration du club" in me.get("/club").text
    # une demande déjà traitée ne peut pas l'être deux fois
    root.post(f"/admin/requests/{rid}/approve", data={"csrf": csrf(root)})
    assert db.query(Club).count() == 3


def test_approve_without_smtp_shows_temp_password(world, db, monkeypatch):
    from app.config import settings
    submit(); rid = db.query(ClubRequest).one().id
    monkeypatch.setattr(settings, "smtp_host", "")
    root = client_for("root@test.org", "rootpassword123")
    r = root.post(f"/admin/requests/{rid}/approve", data={"csrf": csrf(root, f"/admin/requests/{rid}")})
    assert "Mot de passe provisoire" in r.text
    assert db.query(User).filter_by(email="ines@vexin.test").one().must_change_password is True


def test_approve_blocked_by_conflicts(world, db):
    submit(); rid = db.query(ClubRequest).one().id
    root = client_for("root@test.org", "rootpassword123")
    root.post(f"/admin/requests/{rid}/approve", data={"club_name": "Club A", "csrf": csrf(root, f"/admin/requests/{rid}")})
    assert db.query(Club).count() == 2                                       # nom déjà pris : rien créé
    make_user(db, "ines@vexin.test", Role.MEMBER.value, world["b"].id)
    assert "déjà un compte (club Club B)" in root.get(f"/admin/requests/{rid}").text
    root.post(f"/admin/requests/{rid}/approve", data={"csrf": csrf(root, f"/admin/requests/{rid}")})
    db.expire_all()
    assert db.query(Club).count() == 2 and db.get(ClubRequest, rid).status == "pending"


def test_reject_with_and_without_notification(world, db, smtp_outbox):
    submit(); submit(email="b@t.test", club_name="Deuxième"); smtp_outbox.clear()
    a, b = [r.id for r in db.query(ClubRequest).order_by(ClubRequest.id)]
    root = client_for("root@test.org", "rootpassword123")
    root.post(f"/admin/requests/{a}/reject", data={"note": "Club hors France", "notify": "1", "csrf": csrf(root, f"/admin/requests/{a}")})
    assert [m["to"] for m in smtp_outbox] == ["Inès Morel <ines@vexin.test>"] and "Club hors France" in smtp_outbox[0]["text"]
    root.post(f"/admin/requests/{b}/reject", data={"csrf": csrf(root, f"/admin/requests/{b}")})
    assert len(smtp_outbox) == 1
    db.expire_all()
    assert [r.status for r in db.query(ClubRequest).order_by(ClubRequest.id)] == ["rejected", "rejected"]
    assert db.query(Club).count() == 2
    assert "Refusée (2)" in root.get("/admin/requests?status=rejected").text


def test_delete_request_and_escaping(world, db):
    submit(message="<script>alert(1)</script>", club_name="<b>Club</b>")
    rid = db.query(ClubRequest).one().id
    root = client_for("root@test.org", "rootpassword123")
    page = root.get(f"/admin/requests/{rid}").text
    assert "<script>alert(1)</script>" not in page and "<b>Club</b>" not in page
    root.post(f"/admin/requests/{rid}/delete", data={"csrf": csrf(root, f"/admin/requests/{rid}")})
    assert db.query(ClubRequest).count() == 0 and db.query(AuditLog).filter_by(event="club_request.deleted").count() == 1
