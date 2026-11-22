import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import (AuditLog, AuthToken, ClubIntegration, Membership, Outing, OutingForm, Participation,
                        Role, User, UserPref)
from app.services import crypto, helloasso, mailer, memberships, notifications, tokens
from .conftest import (client_for, csrf, enable_helloasso, enable_mailjet, make_user, recipients)

FUTURE = (date.today() + timedelta(days=5)).isoformat()


def post_forgot(email, client=None):
    c = client or TestClient(app)
    return c.post("/forgot", data={"email": email, "csrf": csrf(c, "/forgot")})


def link_from(outbox):
    text = outbox[-1]["messages"][0]["TextPart"]
    return re.search(r"https://escalade\.test(/reset/\S+)", text).group(1)


# --- chiffrement --------------------------------------------------------------

def test_crypto_roundtrip_and_tamper():
    token = crypto.encrypt("s3cret")
    assert "s3cret" not in token and crypto.decrypt(token) == "s3cret"
    assert crypto.decrypt(token[:-4] + "AAAA") is None and crypto.decrypt(None) is None


# --- mot de passe oublié (SMTP classique, indépendant de Mailjet) ----------------

def smtp_link(smtp_outbox):
    return re.search(r"https://escalade\.test(/reset/\S+)", smtp_outbox[-1]["text"]).group(1)


def smtp_to(smtp_outbox):
    return sorted(re.search(r"<(.+)>", m["to"]).group(1) for m in smtp_outbox)


def test_forgot_password_full_flow(world, db, smtp_outbox, outbox):
    r = post_forgot("member_a@test.org")           # le club n'a PAS Mailjet : le SMTP suffit
    assert r.status_code == 200 and "un message vient d" in r.text
    assert smtp_to(smtp_outbox) == ["member_a@test.org"] and outbox == []     # rien ne passe par Mailjet
    assert smtp_outbox[0]["from"].endswith("<no-reply@test.org>")
    path = smtp_link(smtp_outbox)

    old = client_for("member_a@test.org")             # une session ouverte avant la réinitialisation
    assert old.get("/outings").status_code == 200

    c = TestClient(app)
    assert c.get(path).status_code == 200
    r = c.post(path, data={"new": "brand-new-password", "confirm": "brand-new-password", "csrf": csrf(c, path)},
               follow_redirects=False)
    assert r.status_code == 303
    assert c.post(path, data={"new": "another-password1", "confirm": "another-password1",
                              "csrf": csrf(c, "/login")}).status_code == 400      # lien à usage unique
    assert client_for("member_a@test.org", "brand-new-password")
    assert old.get("/outings", follow_redirects=False).headers["location"] == "/login"   # sessions fermées


def test_forgot_works_even_when_club_has_mailjet(world, db, smtp_outbox, outbox):
    enable_mailjet(db, world["a"].id)
    post_forgot("member_a@test.org")
    assert len(smtp_outbox) == 1 and outbox == []     # Mailjet reste réservé aux invitations/notifications


def test_forgot_does_not_reveal_accounts(world, db, smtp_outbox):
    known = post_forgot("member_a@test.org").text
    unknown = post_forgot("nobody@test.org").text
    strip = lambda t: re.sub(r'name="csrf" value="[^"]+"', "", t)
    assert strip(known) == strip(unknown)
    assert smtp_to(smtp_outbox) == ["member_a@test.org"]


def test_forgot_super_admin_now_works(world, db, smtp_outbox):
    post_forgot("root@test.org")                       # le super administrateur n'a pas de club
    assert smtp_to(smtp_outbox) == ["root@test.org"]
    path = smtp_link(smtp_outbox); c = TestClient(app)
    c.post(path, data={"new": "new-root-password1", "confirm": "new-root-password1", "csrf": csrf(c, path)})
    assert client_for("root@test.org", "new-root-password1").get("/outings").status_code == 200


def test_forgot_without_smtp_sends_nothing(world, db, smtp_outbox, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_host", "")
    r = post_forgot("member_a@test.org")
    assert r.status_code == 200 and smtp_outbox == [] and db.query(AuditLog).filter_by(event="password.reset_unavailable").count() == 1
    assert db.query(AuthToken).count() == 0            # pas de jeton créé pour rien


def test_forgot_is_throttled(world, db, smtp_outbox):
    for _ in range(5):
        post_forgot("member_a@test.org")
    assert len(smtp_outbox) == 3
    assert db.query(AuditLog).filter_by(event="password.reset_throttled").count() == 2


def test_reset_token_expired_or_inactive(world, db, smtp_outbox):
    post_forgot("member_a@test.org")
    path = smtp_link(smtp_outbox)
    tok = db.query(AuthToken).one()
    tok.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1); db.commit()
    assert TestClient(app).get(path).status_code == 400
    assert TestClient(app).get("/reset/garbage").status_code == 400
    assert path.split("/")[-1] not in tok.token_hash      # le jeton n'est pas stocké en clair


def test_new_reset_invalidates_previous_link(world, db, smtp_outbox):
    post_forgot("member_a@test.org"); first = smtp_link(smtp_outbox)
    post_forgot("member_a@test.org"); second = smtp_link(smtp_outbox)
    c = TestClient(app)
    assert c.get(first).status_code == 400 and c.get(second).status_code == 200


def test_no_reset_link_without_base_url(world, db, smtp_outbox, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "app_base_url", "")
    post_forgot("member_a@test.org")
    assert smtp_outbox == []


# --- invitation ---------------------------------------------------------------

def test_invite_by_email_and_fallback(world, db, outbox):
    admin = client_for("admin_a@test.org")
    form = lambda e: {"name": "Neo", "email": e, "role": "member", "invite": "1", "csrf": csrf(admin, "/admin/users")}
    r = admin.post("/admin/users", data=form("neo@test.org"))          # pas de Mailjet : mot de passe provisoire
    assert "Mot de passe provisoire" in r.text and outbox == []
    enable_mailjet(db, world["a"].id)
    r = admin.post("/admin/users", data=form("trin@test.org"))
    assert "Invitation envoyée" in r.text and "Mot de passe provisoire :" not in r.text
    path = link_from(outbox)
    c = TestClient(app)
    c.post(path, data={"new": "chosen-password-1", "confirm": "chosen-password-1", "csrf": csrf(c, path)})
    assert client_for("trin@test.org", "chosen-password-1").get("/outings").status_code == 200


def test_invite_failure_falls_back_to_temp_password(world, db, monkeypatch):
    enable_mailjet(db, world["a"].id)
    monkeypatch.setattr(mailer, "_post", lambda *a: (401, {}))
    admin = client_for("admin_a@test.org")
    r = admin.post("/admin/users", data={"name": "Neo", "email": "neo@test.org", "role": "member",
                                         "invite": "1", "csrf": csrf(admin, "/admin/users")})
    assert "n'a pas pu être envoyée" in r.text and "Mot de passe provisoire" in r.text
    assert db.query(User).filter_by(email="neo@test.org").one().must_change_password


# --- notifications de sorties ---------------------------------------------------

def create_outing(c, **extra):
    data = {"title": "Bloc", "site_id": 1, "day": FUTURE, "csrf": csrf(c, "/outings/new"), **extra}
    return c.post("/outings/new", data=data, follow_redirects=False)


def test_new_outing_notifies_club_only(world, db, outbox):
    enable_mailjet(db, world["a"].id); enable_mailjet(db, world["b"].id)
    optout = make_user(db, "quiet_a@test.org", Role.MEMBER.value, world["a"].id)
    db.add(UserPref(user_id=optout.id, notify_email=False, session_epoch=0)); db.commit()
    create_outing(client_for("admin_a@test.org"))
    assert recipients(outbox) == ["member_a@test.org"]       # ni l'auteur, ni l'opt-out, ni le club B
    assert "Nouvelle sortie" in outbox[0]["messages"][0]["Subject"]


def test_no_mail_without_mailjet(world, db, outbox):
    create_outing(client_for("admin_a@test.org"))
    assert outbox == []


def test_change_and_cancel_notify_participants_only(world, db, outbox):
    enable_mailjet(db, world["a"].id)
    admin = client_for("admin_a@test.org")
    create_outing(admin); oid = db.query(Outing).one().id
    member = client_for("member_a@test.org")
    member.post(f"/outings/{oid}/respond", data={"status": "yes", "csrf": csrf(member)})
    outbox.clear()
    same = {"title": "Bloc", "site_id": 1, "day": FUTURE, "csrf": csrf(admin)}
    admin.post(f"/outings/{oid}/edit", data=same)
    assert outbox == []                                       # rien n'a changé : pas d'email
    admin.post(f"/outings/{oid}/edit", data={**same, "meeting_point": "Parking", "csrf": csrf(admin)})
    assert recipients(outbox) == ["member_a@test.org"]
    assert "lieu de rendez-vous" in outbox[-1]["messages"][0]["TextPart"]
    admin.post(f"/outings/{oid}/cancel", data={"csrf": csrf(admin)})
    assert "annulée" in outbox[-1]["messages"][0]["Subject"].lower()


def test_html_email_escapes_content(world, db, outbox):
    enable_mailjet(db, world["a"].id)
    create_outing(client_for("admin_a@test.org"), title="<img src=x onerror=alert(1)>")
    html = outbox[0]["messages"][0]["HTMLPart"]
    assert "<img src=x" not in html and "&lt;img" in html


def test_reminders_once_with_participants(world, db, outbox, monkeypatch):
    enable_mailjet(db, world["a"].id)
    tomorrow = date.today() + timedelta(days=1)
    admin = client_for("admin_a@test.org")
    create_outing(admin, day=tomorrow.isoformat()); oid = db.query(Outing).one().id
    m = client_for("member_a@test.org")
    m.post(f"/outings/{oid}/respond", data={"status": "yes", "csrf": csrf(m)})
    outbox.clear()
    monkeypatch.setattr(notifications.weather, "forecast_for_day", lambda *a: None)
    fake_now = datetime.now(notifications.ZoneInfo("Europe/Paris")).replace(hour=12)
    monkeypatch.setattr(notifications, "datetime", SimpleNamespace(now=lambda tz: fake_now))
    assert notifications.send_reminders() == 1 and recipients(outbox) == ["member_a@test.org"]
    assert notifications.send_reminders() == 0                # jamais deux fois
    fake_now = fake_now.replace(hour=3)                       # pas d'email en pleine nuit
    assert notifications.send_reminders() == 0


def test_notification_preference_toggle(world, db):
    c = client_for("member_a@test.org")
    c.post("/account/notifications", data={"csrf": csrf(c, "/password")})        # case décochée
    assert db.query(UserPref).one().notify_email is False
    c.post("/account/notifications", data={"notify": "1", "csrf": csrf(c, "/password")})
    db.expire_all()
    assert db.query(UserPref).one().notify_email is True


# --- Mailjet : réglages -------------------------------------------------------

MJ = {"api_key": "abcd1234abcd", "api_secret": "supersecretvalue", "from_email": "club@test.org", "from_name": "Mon club"}


def test_mailjet_settings_secret_never_leaks(world, db, outbox):
    c = client_for("admin_a@test.org")
    r = c.post("/admin/integrations/mailjet", data={**MJ, "csrf": csrf(c, "/admin/integrations")}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/admin/integrations").text
    assert "supersecretvalue" not in page and "Configuré" in page
    integ = db.get(ClubIntegration, world["a"].id)
    assert "supersecretvalue" not in integ.mj_api_secret_enc and crypto.decrypt(integ.mj_api_secret_enc) == "supersecretvalue"
    # laisser le secret vide le conserve
    c.post("/admin/integrations/mailjet", data={**MJ, "api_secret": "", "from_name": "Autre", "csrf": csrf(c)})
    db.expire_all()
    assert crypto.decrypt(db.get(ClubIntegration, world["a"].id).mj_api_secret_enc) == "supersecretvalue"


def test_mailjet_validation_and_check_and_test(world, db, outbox):
    c = client_for("admin_a@test.org")
    bad = c.post("/admin/integrations/mailjet", data={**MJ, "from_email": "pas-un-email", "csrf": csrf(c, "/admin/integrations")})
    assert bad.status_code == 400
    c.post("/admin/integrations/mailjet", data={**MJ, "csrf": csrf(c)})
    c.post("/admin/integrations/mailjet/check", data={"csrf": csrf(c)})
    assert outbox[-1]["sandbox"] is True and outbox[-1]["auth"] == ("abcd1234abcd", "supersecretvalue")
    c.post("/admin/integrations/mailjet/test", data={"csrf": csrf(c)})
    assert outbox[-1]["sandbox"] is False and recipients(outbox[-1:]) == ["admin_a@test.org"]   # à soi-même seulement


def test_mailjet_errors_are_reported(world, db, monkeypatch):
    enable_mailjet(db, world["a"].id)
    cfg = mailer.load_config(db, world["a"].id)
    m = mailer.Mail("x@test.org", "X", "S", "t", "<p>t</p>")
    monkeypatch.setattr(mailer, "_post", lambda *a: (401, {}))
    assert mailer.send(cfg, [m]) == (0, "Clés API Mailjet refusées (401).")
    monkeypatch.setattr(mailer, "_post", lambda *a: (400, {"Messages": [{"Status": "error", "Errors": [
        {"ErrorMessage": "Sender not validated"}]}]}))
    assert mailer.send(cfg, [m]) == (0, "Sender not validated")

    def boom(*a):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(mailer, "_post", boom)
    assert mailer.send(cfg, [m])[0] == 0


def test_mailjet_batches_of_50(world, db, outbox):
    enable_mailjet(db, world["a"].id)
    cfg = mailer.load_config(db, world["a"].id)
    mails = [mailer.Mail(f"u{i}@test.org", "U", "S", "t", "<p>t</p>") for i in range(120)]
    assert mailer.send(cfg, mails)[0] == 120 and [len(c["messages"]) for c in outbox] == [50, 50, 20]


def test_header_injection_neutralised(world, db, outbox):
    enable_mailjet(db, world["a"].id)
    cfg = mailer.load_config(db, world["a"].id)
    mailer.send(cfg, [mailer.Mail("x@test.org", "X\r\nBcc: evil@x.org", "Hi\r\nBcc: evil@x.org", "t", "t")])
    msg = outbox[0]["messages"][0]
    assert "\n" not in msg["Subject"] and "\n" not in msg["To"][0]["Name"]


def test_integration_pages_permissions_and_isolation(world, db):
    assert client_for("member_a@test.org").get("/admin/integrations").status_code == 403
    assert client_for("member_a@test.org").get("/admin/memberships").status_code == 403
    a = client_for("admin_a@test.org")
    # un club_id forgé vers le club B est ignoré : on écrit dans le club A
    a.post("/admin/integrations/mailjet", data={**MJ, "club_id": world["b"].id, "csrf": csrf(a, "/admin/integrations")})
    assert db.get(ClubIntegration, world["a"].id) and not db.get(ClubIntegration, world["b"].id)
    root = client_for("root@test.org", "rootpassword123")
    assert root.get(f"/admin/integrations?club={world['a'].id}").status_code == 200
    root.post("/admin/integrations/mailjet", data={**MJ, "club_id": world["b"].id, "csrf": csrf(root, "/admin/integrations")})
    assert db.get(ClubIntegration, world["b"].id).mailjet_ready
    assert root.post("/admin/integrations/mailjet", data={**MJ, "club_id": 999, "csrf": csrf(root)}).status_code == 404


# --- HelloAsso ----------------------------------------------------------------

def item(i, email, state="Processed", typ="Membership", paid=None, amount=2500, first="Ada", last="L"):
    paid = paid or (datetime.now(timezone.utc) - timedelta(days=3))
    return {"id": i, "type": typ, "state": state, "amount": amount, "name": "Adhésion",
            "user": {"firstName": first, "lastName": last}, "payer": {"email": email, "firstName": "Payeur"},
            "order": {"id": 1, "date": paid.isoformat(), "formSlug": "adhesion-2026"}}


class FakeHA:
    """Simule l'API HelloAsso (OAuth2 + formulaires + articles)."""

    def __init__(self, items_by_form=None, forms=None):
        self.items_by_form = items_by_form or {}
        self.forms = forms if forms is not None else [
            {"formType": "Membership", "formSlug": "adhesion-2026", "title": "Adhésion 2026"},
            {"formType": "Event", "formSlug": "sortie-fonta", "title": "Sortie Fontainebleau"}]
        self.calls, self.token_fail, self.expire_once, self.status = [], False, False, 200
        self.HTTPError = httpx.HTTPError

    def _resp(self, code, body, url):
        return httpx.Response(code, json=body, request=httpx.Request("GET", url))

    def post(self, url, data=None, **kw):
        self.calls.append(("token", url))
        if self.token_fail:
            return self._resp(401, {}, url)
        assert data["grant_type"] == "client_credentials"
        return self._resp(200, {"access_token": "tok-%d" % len(self.calls), "expires_in": 1800}, url)

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append(("get", url, dict(params or {})))
        assert headers["Authorization"].startswith("Bearer tok-")
        if self.expire_once:
            self.expire_once = False
            return self._resp(401, {}, url)
        if self.status != 200:
            return self._resp(self.status, {}, url)
        path = url.split("/v5", 1)[1]
        page = (params or {}).get("pageIndex", 1)
        if path.endswith("/forms"):
            rows = self.forms
        else:
            slug = path.split("/")[-2]
            rows = self.items_by_form.get(slug, [])
        size = (params or {}).get("pageSize", 20)
        chunk = rows[(page - 1) * size: page * size]
        return self._resp(200, {"data": chunk, "pagination": {"pageIndex": page, "pageSize": size,
                          "totalCount": len(rows), "totalPages": max(1, -(-len(rows) // size))}}, url)


@pytest.fixture()
def ha(monkeypatch):
    fake = FakeHA()
    monkeypatch.setattr(helloasso, "httpx", fake)
    return fake


def test_helloasso_client_pagination_token_reuse_and_refresh(ha):
    ha.items_by_form["big"] = [item(i, f"u{i}@test.org") for i in range(250)]
    client = helloasso.HelloAsso("client-id-1", "secret")
    items = client.items("mon-club", "Membership", "big")
    assert len(items) == 250 and items[0].email == "u0@test.org"
    assert sum(1 for c in ha.calls if c[0] == "token") == 1          # jeton réutilisé entre les pages
    ha.expire_once = True
    client.forms("mon-club")
    assert sum(1 for c in ha.calls if c[0] == "token") == 2          # 401 : nouveau jeton, puis succès


@pytest.mark.parametrize("status,msg", [(403, "Droits insuffisants"), (404, "introuvable"), (429, "Trop de requêtes")])
def test_helloasso_errors_are_readable(ha, status, msg):
    ha.status = status
    with pytest.raises(helloasso.HelloAssoError, match=msg):
        helloasso.HelloAsso("client-id-1", "secret").forms("mon-club")


def test_helloasso_bad_credentials_and_slug_validation(ha):
    ha.token_fail = True
    with pytest.raises(helloasso.HelloAssoError, match="Identifiants HelloAsso refusés"):
        helloasso.HelloAsso("client-id-1", "bad").forms("mon-club")
    for bad in ("../admin", "Mon Club", "a/b", "", "x?y=1"):
        with pytest.raises(helloasso.HelloAssoError):
            helloasso.HelloAsso("c", "s").forms(bad)
    assert not any("admin" in str(c) for c in ha.calls)             # aucune requête partie avec un slug piégé


def test_membership_validity_rules():
    mk = lambda paid, state="Processed": Membership(state=state, paid_at=paid)
    d = lambda y, m, dd: datetime(y, m, dd, tzinfo=timezone.utc)
    today = date(2026, 3, 1)
    assert memberships.season_start(today) == date(2025, 9, 1) and memberships.season_start(date(2026, 9, 2)) == date(2026, 9, 1)
    assert memberships.is_current(mk(d(2025, 10, 1)), "season", today)
    assert not memberships.is_current(mk(d(2025, 8, 15)), "season", today)
    assert memberships.is_current(mk(d(2025, 4, 1)), "rolling", today)
    assert not memberships.is_current(mk(d(2025, 2, 1)), "rolling", today)
    assert not memberships.is_current(mk(d(2025, 10, 1), "Canceled"), "season", today)
    assert memberships.is_current(mk(d(2025, 10, 1), "Registered"), "season", today)
    assert not memberships.is_current(mk(None), "season", today)


def test_helloasso_settings_check_and_forms(world, db, ha):
    c = client_for("admin_a@test.org")
    r = c.post("/admin/integrations/helloasso", data={"client_id": "client-id-1", "client_secret": "hasecretvalue",
               "org_slug": "mon-club", "csrf": csrf(c, "/admin/integrations")}, follow_redirects=False)
    assert r.status_code == 303 and "hasecretvalue" not in c.get("/admin/integrations").text
    r = c.post("/admin/integrations/helloasso/check", data={"csrf": csrf(c)})
    assert "Connexion réussie" in r.text and "Adhésion 2026" in r.text and "Sortie Fontainebleau" not in r.text
    c.post("/admin/integrations/helloasso/membership", data={"form_slug": "adhesion-2026", "mode": "rolling", "csrf": csrf(c)})
    integ = db.get(ClubIntegration, world["a"].id); db.refresh(integ)
    assert integ.ha_membership_form == "adhesion-2026" and integ.membership_mode == "rolling"
    bad = c.post("/admin/integrations/helloasso", data={"client_id": "client-id-1", "client_secret": "",
                 "org_slug": "../evil", "csrf": csrf(c)})
    assert bad.status_code == 400


def test_membership_sync_page_and_accounts(world, db, ha, outbox):
    enable_helloasso(db, world["a"].id)
    old = datetime.now(timezone.utc) - timedelta(days=800)
    ha.items_by_form["adhesion-2026"] = [
        item(1, "ada@test.org", first="Ada", last="Lovelace"),
        item(2, "member_a@test.org", first="Mem", last="Ber"),                       # a déjà un compte
        item(3, "old@test.org", paid=old, first="Vieux", last="Membre"),
        item(4, "cancel@test.org", state="Canceled"),
        item(5, "ada@test.org", typ="Donation"),                                    # un don n'est pas une adhésion
        {"id": 6, "type": "Membership", "state": "Processed", "payer": {}, "user": {}, "order": {}},   # sans email
    ]
    admin = client_for("admin_a@test.org")
    r = admin.post("/admin/memberships/sync", data={"csrf": csrf(admin, "/admin/memberships")}, follow_redirects=True)
    assert "4 adhésion(s) synchronisée(s)" in r.text
    assert "Ada Lovelace" in r.text and "Expirée" in r.text and "Annulée" in r.text
    assert db.query(Membership).count() == 4
    admin.post("/admin/memberships/sync", data={"csrf": csrf(admin)})                # idempotent
    assert db.query(Membership).count() == 4
    # cotisation visible dans la liste des utilisateurs
    users = admin.get("/admin/users").text
    assert "✅" in users
    # création de comptes sans Mailjet : mots de passe provisoires
    r = admin.post("/admin/memberships/invite", data={"email": "ada@test.org", "csrf": csrf(admin)})
    assert "Comptes créés" in r.text and db.query(User).filter_by(email="ada@test.org").one().club_id == world["a"].id
    # avec Mailjet : invitations par email, "tous" ignore les comptes existants
    enable_mailjet(db, world["a"].id)
    r = admin.post("/admin/memberships/invite", data={"email": "__all__", "csrf": csrf(admin)})
    assert "compte(s) créé(s)" in r.text
    assert sorted(recipients(outbox)) == ["cancel@test.org", "old@test.org"]
    # on ne crée pas de compte pour un email qui n'est pas une adhésion importée
    admin.post("/admin/memberships/invite", data={"email": "intrus@test.org", "csrf": csrf(admin)})
    assert db.query(User).filter_by(email="intrus@test.org").count() == 0


def test_membership_sync_errors_and_isolation(world, db, ha):
    admin = client_for("admin_a@test.org")
    r = admin.post("/admin/memberships/sync", data={"csrf": csrf(admin, "/admin/memberships")})
    assert r.status_code == 400 and "Choisissez d" in r.text
    enable_helloasso(db, world["a"].id)
    ha.status = 403
    r = admin.post("/admin/memberships/sync", data={"csrf": csrf(admin)})
    assert r.status_code == 400 and "Droits insuffisants" in r.text
    ha.status = 200
    ha.items_by_form["adhesion-2026"] = [item(1, "ada@test.org")]
    # un administrateur du club B ne peut pas toucher aux adhésions du club A
    b = client_for("admin_b@test.org")
    assert b.post("/admin/memberships/sync", data={"club_id": world["a"].id, "csrf": csrf(b, "/admin/memberships")}).status_code == 400
    assert db.query(Membership).count() == 0
    assert "Ada" not in b.get(f"/admin/memberships?club={world['a'].id}").text


# --- paiement des sorties ---------------------------------------------------------

def test_outing_payments(world, db, ha):
    enable_helloasso(db, world["a"].id)
    admin = client_for("admin_a@test.org")
    create_outing(admin); oid = db.query(Outing).one().id
    m = client_for("member_a@test.org")
    m.post(f"/outings/{oid}/respond", data={"status": "yes", "csrf": csrf(m)})
    assert m.get(f"/outings/{oid}/payments").status_code == 403          # réservé aux administrateurs
    assert client_for("admin_b@test.org").get(f"/outings/{oid}/payments").status_code == 404
    assert "Sortie Fontainebleau" in admin.get(f"/outings/{oid}/payments").text
    assert admin.post(f"/outings/{oid}/payments/link", data={"form": "Event|inconnu", "csrf": csrf(admin)}).status_code == 400
    assert admin.post(f"/outings/{oid}/payments/link", data={"form": "Membership|adhesion-2026", "csrf": csrf(admin)}).status_code == 400
    ha.items_by_form["sortie-fonta"] = [
        item(1, "member_a@test.org", typ="Payment", amount=1500),
        item(2, "member_a@test.org", typ="Payment", amount=500),
        item(3, "stranger@test.org", typ="Payment", amount=1500, first="Eve"),
        item(4, "admin_a@test.org", typ="Payment", state="Canceled"),
    ]
    admin.post(f"/outings/{oid}/payments/link", data={"form": "Event|sortie-fonta", "csrf": csrf(admin)})
    assert db.get(OutingForm, oid).form_slug == "sortie-fonta"
    page = admin.get(f"/outings/{oid}/payments").text
    assert "20.00 €" in page and "stranger@test.org" in page and "Paiements non rattachés" in page
    assert "admin_a@test.org" not in page.split("Paiements non rattachés")[-1]      # paiement annulé ignoré
    calls = len(ha.calls); admin.get(f"/outings/{oid}/payments")
    assert len(ha.calls) == calls                                                  # cache 60 s
    admin.get(f"/outings/{oid}/payments?refresh=1"); assert len(ha.calls) > calls
    admin.post(f"/outings/{oid}/payments/unlink", data={"csrf": csrf(admin)})
    assert db.get(OutingForm, oid) is None
    admin.post(f"/outings/{oid}/payments/link", data={"form": "Event|sortie-fonta", "csrf": csrf(admin)})
    assert admin.post(f"/outings/{oid}/delete", data={"csrf": csrf(admin)}, follow_redirects=False).status_code == 303


# --- CLI ----------------------------------------------------------------------------

def test_cli_reset_password(world, db, capsys):
    from app import cli
    c = client_for("root@test.org", "rootpassword123")
    assert cli.main(["x", "reset-password", "nobody@test.org"]) == 1
    assert cli.main(["x", "reset-password", "root@test.org"]) == 0
    temp = re.search(r": (\S+)\n", capsys.readouterr().out).group(1)
    assert c.get("/outings", follow_redirects=False).headers["location"] == "/login"   # session fermée
    c2 = client_for("root@test.org", temp)
    assert c2.get("/outings", follow_redirects=False).headers["location"] == "/password"
