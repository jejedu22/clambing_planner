import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.db import Base
from app.main import app
from app.migrations import upgrade_schema
from app.models import AuditLog, AuthToken, Club, ClubIntegration, Role, User
from app.services import people
from .conftest import client_for, csrf, enable_mailjet, make_user, recipients


# --- migration ------------------------------------------------------------------

def test_schema_upgrade_adds_columns_and_backfills_names(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (email, name, password_hash, role, is_active, must_change_password, created_at) "
                       "VALUES ('jean@x.org', 'Jean Dupont', 'h', 'member', 1, 0, '2026-01-01'), "
                       "('solo@x.org', 'Madonna', 'h', 'member', 1, 0, '2026-01-01')"))
        for col in ("first_name", "last_name", "phone"):          # simule une base créée avant cette version
            c.execute(text(f"ALTER TABLE users DROP COLUMN {col}"))
    upgrade_schema(engine)
    with engine.connect() as c:
        rows = {r[0]: r[1:] for r in c.execute(text("SELECT email, first_name, last_name, phone FROM users"))}
    assert rows["jean@x.org"] == ("Jean", "Dupont", None) and rows["solo@x.org"] == ("Madonna", "", None)
    upgrade_schema(engine)                                        # idempotent
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM users")).scalar() == 2


def test_display_name_is_kept_in_sync(db):
    u = User(email="a@x.org", name="Marie Curie", password_hash="h")
    db.add(u); db.commit()
    assert (u.first_name, u.last_name) == ("Marie", "Curie")
    u.first_name, u.last_name = "Pierre", "Curie-Sklodowska"; db.commit()
    assert u.name == "Pierre Curie-Sklodowska"
    u.last_name = ""; db.commit()
    assert u.name == "Pierre"


# --- validation ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("06 12 34 56 78", "+33612345678"), ("+33 6 12 34 56 78", "+33612345678"), ("0033612345678", "+33612345678"),
    ("(06)12.34.56.78", "+33612345678"), ("+1 (415) 555-2671", "+14155552671"), ("", None), ("   ", None)])
def test_phone_normalisation(raw, expected):
    assert people.normalize_phone(raw) == expected


@pytest.mark.parametrize("bad", ["12345", "abc", "06 12 34", "+0612345678", "06123456789012345", "<script>"])
def test_phone_rejected(bad):
    with pytest.raises(people.ValidationError):
        people.normalize_phone(bad)


def test_person_validation():
    assert people.validate_person(" Ada  ", "Lovelace", "ADA@Test.org ", "0612345678") == ("Ada", "Lovelace", "ada@test.org", "+33612345678")
    assert people.validate_person("", "Solo", "s@t.org", "")[:2] == ("", "Solo")
    for args in (("", "", "a@b.org", ""), ("A", "B", "pas-un-email", ""), ("A" * 56, "B", "a@b.org", ""), ("A\x00", "B", "a@b.org", "")):
        with pytest.raises(people.ValidationError):
            people.validate_person(*args)
    assert people.format_phone("+33612345678") == "06 12 34 56 78" and people.format_phone("+14155552671") == "+14155552671"


# --- création, liste, modification --------------------------------------------------

def test_create_user_with_phone_and_list(world, db):
    c = client_for("admin_a@test.org")
    r = c.post("/admin/users", data={"first_name": "Camille", "last_name": "Martin", "email": "cam@test.org", "phone": "06 12 34 56 78",
                                     "role": "member", "csrf": csrf(c, "/admin/users")})
    assert r.status_code == 200
    u = db.query(User).filter_by(email="cam@test.org").one()
    assert (u.first_name, u.last_name, u.phone, u.name) == ("Camille", "Martin", "+33612345678", "Camille Martin")
    assert "Martin Camille" in c.get("/admin/users").text and "06 12 34 56 78" in c.get("/admin/users").text
    bad = c.post("/admin/users", data={"first_name": "A", "last_name": "B", "email": "b@test.org", "phone": "nope",
                                       "role": "member", "csrf": csrf(c)})
    assert bad.status_code == 400 and "téléphone" in bad.text


def test_user_list_search_filter_and_pagination(world, db):
    for i in range(60):
        make_user(db, f"user{i:02d}@a.test", Role.MEMBER.value, world["a"].id)
    make_user(db, "x@b.test", Role.MEMBER.value, world["b"].id)
    db.query(User).filter_by(email="user07@a.test").one().phone = "+33698765432"; db.commit()
    c = client_for("admin_a@test.org")
    assert "62 utilisateur(s)" in c.get("/admin/users").text
    assert "Page 1 / 2" in c.get("/admin/users").text and "Page 2 / 2" in c.get("/admin/users?page=2").text
    assert "user07@a.test" in c.get("/admin/users?q=0698").text and "user08@a.test" not in c.get("/admin/users?q=0698").text.replace("0698", "")
    assert "x@b.test" not in c.get("/admin/users?q=x@b").text                      # jamais un autre club
    assert "1 utilisateur(s)" in c.get("/admin/users?q=user07").text
    assert "100%" not in c.get("/admin/users?q=%25").text                             # % et _ ne sont pas des jokers
    root = client_for("root@test.org", "rootpassword123")
    assert "x@b.test" in root.get(f"/admin/users?club={world['b'].id}").text
    assert "user07@a.test" not in root.get(f"/admin/users?club={world['b'].id}").text
    assert client_for("member_a@test.org").get("/admin/users").status_code == 403


def test_edit_user_full(world, db):
    c = client_for("admin_a@test.org")
    target = db.query(User).filter_by(email="member_a@test.org").one()
    assert c.get(f"/admin/users/{target.id}/edit").status_code == 200
    post = lambda **kw: c.post(f"/admin/users/{target.id}/edit", data={
        "first_name": "Nina", "last_name": "Simone", "email": "member_a@test.org", "phone": "0611223344",
        "role": "club_admin", "active": "1", "csrf": csrf(c, f"/admin/users/{target.id}/edit"), **kw}, follow_redirects=False)
    assert post().status_code == 303
    db.refresh(target)
    assert (target.name, target.phone, target.role) == ("Nina Simone", "+33611223344", "club_admin")
    assert post(email="admin_b@test.org").status_code == 400                           # email déjà pris
    assert post(role="super_admin").status_code == 400                                 # pas d'élévation
    assert post(phone="xx").status_code == 400
    assert post(active="").status_code == 303 and db.refresh(target) is None and target.is_active is False


def test_edit_email_invalidates_pending_links_and_audits_masked(world, db, smtp_outbox):
    c = TestClient(app)
    c.post("/forgot", data={"email": "member_a@test.org", "csrf": csrf(c, "/forgot")})
    assert db.query(AuthToken).filter(AuthToken.used_at.is_(None)).count() == 1
    admin = client_for("admin_a@test.org"); t = db.query(User).filter_by(email="member_a@test.org").one()
    admin.post(f"/admin/users/{t.id}/edit", data={"first_name": "M", "last_name": "A", "email": "new.mail@test.org",
               "role": "member", "active": "1", "csrf": csrf(admin, f"/admin/users/{t.id}/edit")})
    db.expire_all()
    assert db.query(AuthToken).filter(AuthToken.used_at.is_(None)).count() == 0
    ev = db.query(AuditLog).filter_by(event="user.email_changed").one()
    assert "m***@test.org -> n***@test.org" == ev.detail and "member_a" not in json.dumps([ev.detail, ev.actor_label])


def test_edit_permissions(world, db):
    a = client_for("admin_a@test.org")
    other = db.query(User).filter_by(email="admin_b@test.org").one()
    root = db.query(User).filter_by(role=Role.SUPER_ADMIN.value).one()
    me = db.query(User).filter_by(email="admin_a@test.org").one()
    assert a.get(f"/admin/users/{other.id}/edit").status_code == 404                  # autre club
    assert a.get(f"/admin/users/{root.id}/edit").status_code == 404                   # super administrateur
    assert a.get(f"/admin/users/{me.id}/edit").status_code == 403                     # pas soi-même ici
    assert client_for("member_a@test.org").get(f"/admin/users/{me.id}/edit").status_code == 403
    assert client_for("root@test.org", "rootpassword123").get(f"/admin/users/{other.id}/edit").status_code == 200


def test_self_profile(world, db):
    c = client_for("member_a@test.org")
    page = c.get("/password").text
    assert 'name="first_name"' in page and 'value="member_a@test.org" disabled' in page   # email non modifiable ici
    c.post("/account/profile", data={"first_name": "Lou", "last_name": "Reed", "phone": "06 00 00 00 01",
                                     "email": "hacker@test.org", "csrf": csrf(c, "/password")})
    u = db.query(User).filter_by(id=db.query(User).filter_by(role="member", club_id=world["a"].id).first().id).one()
    db.refresh(u)
    assert (u.name, u.phone, u.email) == ("Lou Reed", "+33600000001", "member_a@test.org")
    c.post("/account/profile", data={"first_name": "", "last_name": "", "csrf": csrf(c, "/password")})
    db.refresh(u); assert u.name == "Lou Reed"                                            # refus : au moins un nom


# --- export ---------------------------------------------------------------------------

def read_csv(resp):
    assert resp.content.startswith(b"\xef\xbb\xbf")                                      # BOM pour Excel
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")), delimiter=";"))


def test_export_scoped_safe_and_audited(world, db):
    evil = make_user(db, "evil@a.test", Role.MEMBER.value, world["a"].id)
    evil.first_name, evil.last_name, evil.phone = "=HYPERLINK(\"http://x\")", "@SUM(1)", "+33611111111"; db.commit()
    make_user(db, "secret@b.test", Role.MEMBER.value, world["b"].id)
    c = client_for("admin_a@test.org")
    r = c.get("/admin/users/export")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "attachment" in r.headers["content-disposition"] and "club-a" in r.headers["content-disposition"]
    rows = read_csv(r)
    emails = {x["email"] for x in rows}
    assert "secret@b.test" not in emails and "evil@a.test" in emails and "root@test.org" not in emails
    row = next(x for x in rows if x["email"] == "evil@a.test")
    assert row["prenom"].startswith("'=") and row["nom"].startswith("'@") and row["telephone"] == "+33611111111"
    assert row["club"] == "Club A" and row["actif"] == "oui" and row["role"] == "member"
    assert db.query(AuditLog).filter_by(event="users.exported").count() == 1
    assert "secret@b.test" not in c.get(f"/admin/users/export?club={world['b'].id}").text           # ?club= ignoré
    assert [x["email"] for x in read_csv(c.get("/admin/users/export?q=evil"))] == ["evil@a.test"]
    root = client_for("root@test.org", "rootpassword123")
    assert {"secret@b.test", "root@test.org"} <= {x["email"] for x in read_csv(root.get("/admin/users/export"))}
    assert {x["email"] for x in read_csv(root.get(f"/admin/users/export?club={world['b'].id}"))} == {"admin_b@test.org", "secret@b.test"}
    assert client_for("member_a@test.org").get("/admin/users/export").status_code == 403
    assert TestClient(app).get("/admin/users/export", follow_redirects=False).status_code == 303


# --- import ---------------------------------------------------------------------------

def upload(c, content, club_id="", step="preview", invite="", csv_text=""):
    data = {"step": step, "club_id": club_id, "invite": invite, "csrf": csrf(c, "/admin/users/import")}
    if step == "confirm":
        data["csv_text"] = csv_text
        return c.post("/admin/users/import", data=data)
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    return c.post("/admin/users/import", data=data, files={"file": ("u.csv", raw, "text/csv")})


def confirm_text(resp):
    import html, re
    return html.unescape(re.search(r'<textarea name="csv_text" hidden>(.*?)</textarea>', resp.text, re.S).group(1))


CSV = ("prenom;nom;email;telephone;role\n"
       "Ada;Lovelace;ada@test.org;06 12 34 56 78;member\n"
       "Alan;Turing;alan@test.org;;club_admin\n")


def test_import_preview_writes_nothing_then_confirm_creates(world, db):
    c = client_for("admin_a@test.org")
    before = db.query(User).count()
    r = upload(c, CSV)
    assert r.status_code == 200 and "rien n'est encore enregistré" in r.text and "2 à créer" in r.text
    assert db.query(User).count() == before                                             # simulation pure
    r = upload(c, None, step="confirm", csv_text=confirm_text(r))
    assert "2 créé(s)" in r.text and "Mots de passe provisoires" in r.text
    ada = db.query(User).filter_by(email="ada@test.org").one()
    assert (ada.first_name, ada.last_name, ada.phone, ada.club_id, ada.must_change_password) == ("Ada", "Lovelace", "+33612345678", world["a"].id, True)
    assert db.query(User).filter_by(email="alan@test.org").one().role == "club_admin"
    ev = db.query(AuditLog).filter_by(event="users.imported").one()
    assert "créés=2" in ev.detail and "ada@test.org" not in json.dumps(ev.detail)


def test_import_update_is_prudent(world, db):
    c = client_for("admin_a@test.org")
    target = db.query(User).filter_by(email="member_a@test.org").one()
    target.phone = "+33699999999"; db.commit()
    r = upload(c, "prenom;nom;email;telephone;role\nNina;Simone;MEMBER_A@test.org;;club_admin\n")
    assert "1 à modifier" in r.text and "modifie : prénom, nom" in r.text
    upload(c, None, step="confirm", csv_text=confirm_text(r))
    db.refresh(target)
    assert (target.first_name, target.last_name) == ("Nina", "Simone")
    assert target.phone == "+33699999999" and target.role == "member"                    # vide n'efface pas ; rôle intact
    r = upload(c, "prenom;nom;email\nNina;Simone;member_a@test.org\n")
    assert "1 inchangé" in r.text and "Confirmer l'import" not in r.text


def test_import_errors_per_row(world, db):
    c = client_for("admin_a@test.org")
    content = ("prenom;nom;email;telephone;role\n"
               "Ok;Row;ok@test.org;;\n"
               "Dup;Row;ok@test.org;;\n"
               "Bad;Mail;pas-un-email;;\n"
               "Bad;Phone;phone@test.org;123;\n"
               "Bad;Role;role@test.org;;super_admin\n"
               ";;noname@test.org;;\n"
               "Other;Club;admin_b@test.org;;\n"
               "Root;Admin;root@test.org;;\n")
    r = upload(c, content)
    assert "1 à créer" in r.text and "7 erreur(s)" in r.text
    for msg in ("en double", "Adresse email invalide", "téléphone invalide", "Rôle", "au moins un prénom ou un nom", "hors de ce club"):
        assert msg in r.text
    upload(c, None, step="confirm", csv_text=confirm_text(r))
    assert db.query(User).filter_by(email="ok@test.org").count() == 1 and db.query(User).filter_by(email="role@test.org").count() == 0
    root = db.query(User).filter_by(email="root@test.org").one()
    assert root.role == Role.SUPER_ADMIN.value and root.club_id is None and root.first_name != "Root"   # intact


def test_import_formats_and_encodings(world, db):
    c = client_for("admin_a@test.org")
    comma = "Prénom,Nom,E-mail,Téléphone\nÉlise,Dupré,elise@test.org,0611111111\n\n,,,\n"
    r = upload(c, comma); assert "1 à créer" in r.text and "Élise" in r.text
    tab = "prenom\tnom\tcourriel\nTab\tUser\ttab@test.org\n"
    assert "1 à créer" in upload(c, tab).text
    cp1252 = "prenom;nom;email\nRené;Müller;rene@test.org\n".encode("cp1252")                 # export Excel français
    assert "René" in upload(c, cp1252).text and "Müller" in upload(c, cp1252).text
    assert "BOM" not in upload(c, "﻿" + CSV).text and "2 à créer" in upload(c, "﻿" + CSV).text


@pytest.mark.parametrize("content,msg", [
    ("", "vide"), ("prenom;nom\nA;B\n", "Colonnes attendues"), ("email\nx@y.org\n", "Colonnes attendues"),
    ("prenom;nom;email\n", "Aucune ligne"), ("x" * 600_000, "volumineux"),
    ("prenom;nom;email\n" + "\n".join(f"A;B;u{i}@t.org" for i in range(2001)), "Trop de lignes")])
def test_import_file_errors(world, content, msg):
    r = upload(client_for("admin_a@test.org"), content)
    assert r.status_code == 400 and msg in r.text


def test_import_confirm_revalidates_tampered_payload(world, db):
    c = client_for("admin_a@test.org")
    tampered = ("prenom;nom;email;role\nEvil;Admin;evil@test.org;super_admin\n"
                "Take;Over;admin_b@test.org;member\nNo;Role;root@test.org;member\n")
    r = upload(c, None, step="confirm", csv_text=tampered)
    assert "3 erreur" not in r.text and "0 créé(s)" in r.text
    assert db.query(User).filter_by(email="evil@test.org").count() == 0
    assert db.query(User).filter_by(email="admin_b@test.org").one().club_id == world["b"].id
    assert db.query(User).filter_by(role=Role.SUPER_ADMIN.value).count() == 1


def test_import_club_scoping(world, db):
    a = client_for("admin_a@test.org")
    r = upload(a, CSV, club_id=str(world["b"].id)); r = upload(a, None, step="confirm", csv_text=confirm_text(r), club_id=str(world["b"].id))
    assert db.query(User).filter_by(email="ada@test.org").one().club_id == world["a"].id    # club forgé ignoré
    root = client_for("root@test.org", "rootpassword123")
    assert upload(root, CSV).status_code == 400                                              # le super admin doit choisir
    r = upload(root, "prenom;nom;email\nBea;B;bea@test.org\n", club_id=str(world["b"].id))
    upload(root, None, step="confirm", csv_text=confirm_text(r), club_id=str(world["b"].id))
    assert db.query(User).filter_by(email="bea@test.org").one().club_id == world["b"].id
    assert client_for("member_a@test.org").get("/admin/users/import").status_code == 403
    assert client_for("member_a@test.org").post("/admin/users/import", data={"csrf": "x"}).status_code == 403


def test_import_with_invitations(world, db, outbox):
    enable_mailjet(db, world["a"].id)
    c = client_for("admin_a@test.org")
    r = upload(c, CSV, invite="1"); assert "invitation par email" in r.text
    r = upload(c, None, step="confirm", csv_text=confirm_text(r), invite="1")
    assert "Mots de passe provisoires" not in r.text and "invitations partent" in r.text
    assert recipients(outbox) == ["ada@test.org", "alan@test.org"]
    assert all("/reset/" in call_msg["TextPart"] for call in outbox for call_msg in call["messages"])
    assert db.query(User).filter_by(email="ada@test.org").one().must_change_password is False


def test_export_import_roundtrip_changes_nothing(world, db):
    c = client_for("admin_a@test.org")
    exported = c.get("/admin/users/export").content
    r = upload(c, exported)
    assert "0 à créer" in r.text and "0 à modifier" in r.text and "0 erreur" in r.text


def test_import_template_download(world):
    r = client_for("admin_a@test.org").get("/admin/users/import/template")
    assert r.status_code == 200 and b"prenom;nom;email" in r.content
