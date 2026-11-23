import html
import logging
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditLog, Outing, OutingGuest, OutingShareLink
from app.services import notifications
from .conftest import client_for, csrf, enable_mailjet, recipients

GUEST = {"first_name": "Patti", "last_name": "Smith", "email": "patti@ext.test", "phone": "", "consent": "1"}


def make_outing(db, admin, day=None, **extra):
    day = day or date.today() + timedelta(days=5)
    admin.post("/outings/new", data={"title": "Bloc ouvert", "site_id": 1, "day": day.isoformat(),
                                     "meeting_point": "Parking", "csrf": csrf(admin, "/outings/new"), **extra})
    return db.query(Outing).order_by(Outing.id.desc()).first()


def share(admin, oid):
    admin.post(f"/outings/{oid}/share", data={"csrf": csrf(admin, f"/outings/{oid}")})
    url = html.unescape(re.search(r'id="share-link" value="([^"]+)"', admin.get(f"/outings/{oid}").text).group(1))
    return "/" + url.split("/", 3)[3]


def register(path, client=None, **over):
    c = client or TestClient(app)
    return c.post(path, data={**GUEST, **over, "csrf": csrf(c, path)})


def test_guest_registers_and_gets_confirmation(world, db, outbox):
    enable_mailjet(db, world["a"].id)
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin); path = share(admin, o.id)
    member = client_for("member_a@test.org")
    member.post(f"/outings/{o.id}/respond", data={"status": "yes", "csrf": csrf(member)})
    page = TestClient(app).get(path).text
    assert "Bloc ouvert" in page and "Club A vous invite" in page and 'content="no-referrer"' in page
    assert "member_a" not in page and "Je viens" not in page                    # pas de noms de participants
    outbox.clear()
    r = register(path)
    assert r.status_code == 200 and "Vous êtes inscrit(e)" in r.text
    g = db.query(OutingGuest).one()
    assert (g.name, g.email, g.outing_id) == ("Patti Smith", "patti@ext.test", o.id)
    assert recipients(outbox) == ["patti@ext.test"]
    text = outbox[0]["messages"][0]["TextPart"]
    assert "Inscription confirmée" in outbox[0]["messages"][0]["Subject"] and "/invite/annuler/" in text
    detail = admin.get(f"/outings/{o.id}").text
    assert "Invités (1)" in detail and "patti@ext.test" in detail                 # l'admin voit les coordonnées
    mpage = member.get(f"/outings/{o.id}").text
    assert "Patti Smith" in mpage and "patti@ext.test" not in mpage               # un membre voit le nom seulement
    assert "dont 1 invité(s)" in admin.get("/").text


def test_guest_cancels_with_personal_link(world, db, outbox):
    enable_mailjet(db, world["a"].id)
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin); path = share(admin, o.id)
    register(path)
    cancel = re.search(r"https://escalade\.test(/invite/annuler/\S+)", outbox[-1]["messages"][0]["TextPart"]).group(1)
    c = TestClient(app)
    assert "Se désinscrire" in c.get(cancel).text
    assert "Désinscription faite" in c.post(cancel, data={"csrf": csrf(c, cancel)}).text
    assert db.query(OutingGuest).count() == 0
    assert c.get(cancel).status_code == 404                                       # lien à usage unique


def test_capacity_counts_members_and_guests(world, db):
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin, max_participants="2"); path = share(admin, o.id)
    admin.post(f"/outings/{o.id}/respond", data={"status": "yes", "csrf": csrf(admin)})
    assert "1 restante(s)" in TestClient(app).get(path).text
    assert register(path).status_code == 200
    r = register(path, email="second@ext.test")
    assert r.status_code in (200, 409) and db.query(OutingGuest).count() == 1     # complet
    assert "La sortie est complète" in TestClient(app).get(path).text
    member = client_for("member_a@test.org")                                      # un membre ne peut plus dire oui
    member.post(f"/outings/{o.id}/respond", data={"status": "yes", "csrf": csrf(member)})
    from app.models import Participation
    assert db.query(Participation).filter_by(status="yes").count() == 1


def test_duplicates_members_and_validation(world, db):
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin); path = share(admin, o.id)
    assert register(path).status_code == 200
    r = register(path, email="PATTI@ext.test")
    assert r.status_code == 400 and "déjà inscrite" in r.text
    r = register(path, email="member_a@test.org")
    assert r.status_code == 400 and "connectez-vous" in r.text
    for over, msg in ((dict(consent=""), "accepter"), (dict(email="nope"), "email invalide"), (dict(last_name=""), "prénom et votre nom")):
        assert msg in register(path, **{"email": "x@ext.test", **over}).text, over
    r = register(path, email="bot@ext.test", website="http://spam")
    assert "inscrit" in r.text and db.query(OutingGuest).count() == 1             # champ piège
    assert TestClient(app).post(path, data=GUEST).status_code == 403             # CSRF


def test_link_lifecycle(world, db):
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin)
    assert "Créer un lien de partage" in admin.get(f"/outings/{o.id}").text
    first = share(admin, o.id)
    second = share(admin, o.id)                                                  # régénérer : l'ancien cesse de marcher
    assert first != second and TestClient(app).get(first).status_code == 404 and TestClient(app).get(second).status_code == 200
    admin.post(f"/outings/{o.id}/share/revoke", data={"csrf": csrf(admin, f"/outings/{o.id}")})
    assert TestClient(app).get(second).status_code == 404
    third = share(admin, o.id)
    admin.post(f"/outings/{o.id}/cancel", data={"csrf": csrf(admin)})            # sortie annulée
    assert TestClient(app).get(third).status_code == 404 and register(third).status_code == 404
    past = make_outing(db, admin, day=date.today() + timedelta(days=2)); p = share(admin, past.id)
    past.day = date.today() - timedelta(days=1); db.commit()                     # sortie passée
    assert TestClient(app).get(p).status_code == 404
    assert TestClient(app).get("/sortie/inventé").status_code == 404
    link = db.query(OutingShareLink).first()
    assert first.rsplit("/", 1)[1] not in link.token_hash + link.token_enc


def test_guests_notified_of_changes_and_reminder(world, db, outbox, monkeypatch):
    enable_mailjet(db, world["a"].id)
    admin = client_for("admin_a@test.org")
    o = make_outing(db, admin, day=date.today() + timedelta(days=1)); path = share(admin, o.id)
    register(path); outbox.clear()
    admin.post(f"/outings/{o.id}/edit", data={"title": "Bloc ouvert", "site_id": 1, "day": o.day.isoformat(),
                                              "meeting_point": "Autre parking", "csrf": csrf(admin)})
    assert "patti@ext.test" in recipients(outbox) and "/invite/annuler/" in outbox[-1]["messages"][0]["TextPart"]
    outbox.clear()
    monkeypatch.setattr(notifications.weather, "forecast_for_day", lambda *a: None)
    noon = datetime.now(notifications.ZoneInfo("Europe/Paris")).replace(hour=12)
    monkeypatch.setattr(notifications, "datetime", SimpleNamespace(now=lambda tz: noon))
    notifications.send_reminders()
    assert "patti@ext.test" in recipients(outbox) and "Rappel demain" in outbox[-1]["messages"][0]["Subject"]
    outbox.clear()
    admin.post(f"/outings/{o.id}/cancel", data={"csrf": csrf(admin)})
    assert "patti@ext.test" in recipients(outbox) and "annulée" in outbox[-1]["messages"][0]["Subject"].lower()


def test_guests_email_falls_back_to_smtp(world, db, smtp_outbox, outbox):
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin); path = share(admin, o.id)
    register(path)                                                               # club sans Mailjet
    assert outbox == [] and [m["to"] for m in smtp_outbox] == ["Patti Smith <patti@ext.test>"]


def test_permissions_and_removal(world, db):
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin); path = share(admin, o.id)
    register(path); gid = db.query(OutingGuest).one().id
    m, b = client_for("member_a@test.org"), client_for("admin_b@test.org")
    assert "Partager avec des non-membres" not in m.get(f"/outings/{o.id}").text
    assert m.post(f"/outings/{o.id}/share", data={"csrf": csrf(m)}).status_code == 403
    assert b.post(f"/outings/{o.id}/share", data={"csrf": csrf(b)}).status_code == 404
    assert b.post(f"/outings/{o.id}/guests/{gid}/remove", data={"csrf": csrf(b)}).status_code == 404
    admin.post(f"/outings/{o.id}/guests/{gid}/remove", data={"csrf": csrf(admin)})
    assert db.query(OutingGuest).count() == 0
    assert db.query(AuditLog).filter_by(event="outing_guest.removed").count() == 1
    register(path, email="again@ext.test")
    admin.post(f"/outings/{o.id}/delete", data={"csrf": csrf(admin)})          # suppression : invités et liens aussi
    assert db.query(OutingGuest).count() == 0 and db.query(OutingShareLink).count() == 0


def test_tokens_never_logged(world, db, caplog, outbox):
    enable_mailjet(db, world["a"].id)
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin); path = share(admin, o.id)
    caplog.set_level(logging.DEBUG)
    TestClient(app).get(path); register(path)
    cancel = re.search(r"(/invite/annuler/\S+)", outbox[-1]["messages"][0]["TextPart"]).group(1)
    TestClient(app).get(cancel)
    logs = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert path.rsplit("/", 1)[1] not in logs and cancel.rsplit("/", 1)[1] not in logs
    assert "/sortie/[masqué]" in logs and "/invite/annuler/[masqué]" in logs


def test_payments_include_guests(world, db, monkeypatch):
    from tests.test_integrations import FakeHA, item
    from app.services import helloasso
    from .conftest import enable_helloasso
    fake = FakeHA(); monkeypatch.setattr(helloasso, "httpx", fake)
    enable_helloasso(db, world["a"].id)
    admin = client_for("admin_a@test.org"); o = make_outing(db, admin); path = share(admin, o.id)
    register(path)
    fake.items_by_form["sortie-fonta"] = [item(1, "patti@ext.test", typ="Payment", amount=1200)]
    admin.post(f"/outings/{o.id}/payments/link", data={"form": "Event|sortie-fonta", "csrf": csrf(admin)})
    page = admin.get(f"/outings/{o.id}/payments").text
    assert "Patti Smith" in page and "12.00 €" in page and "Paiements non rattachés" not in page
