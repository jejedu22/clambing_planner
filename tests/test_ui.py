from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuthToken, Outing, Participation, Role, User
from app.routes.dashboard import week_title
from .conftest import client_for, csrf, make_user


def set_theme(c, value, next="/"):
    return c.post("/theme", data={"theme": value, "next": next, "csrf": csrf(c, "/login")}, follow_redirects=False)


def test_theme_choice_persists_and_renders(world):
    c = TestClient(app)
    assert 'data-theme=' not in c.get("/login").text and "light dark" in c.get("/login").text   # auto par défaut
    r = set_theme(c, "dark", "/login")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    cookie = r.headers["set-cookie"]
    assert "theme=dark" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=31536000" in cookie
    page = c.get("/login").text
    assert '<html lang="fr" data-theme="dark">' in page and 'value="dark" aria-pressed="true"' in page
    set_theme(c, "light")
    assert 'data-theme="light"' in c.get("/login").text
    r = set_theme(c, "auto")
    assert 'theme=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    assert "data-theme" not in c.get("/login").text.split("<head>")[0]


def test_theme_rejects_bad_values_and_open_redirects(world):
    c = TestClient(app)
    for bad in ("https://evil.example", "//evil.example", "/\\evil"):
        assert set_theme(c, "dark", bad).headers["location"] == "/"
    set_theme(c, "<script>")
    assert "data-theme" not in c.get("/login").text.split("<head>")[0]
    c.cookies.set("theme", '"><script>alert(1)</script>')
    assert "<script>alert(1)" not in c.get("/login").text                  # cookie forgé : ignoré
    assert TestClient(app).post("/theme", data={"theme": "dark"}).status_code == 403   # CSRF exigé


def test_theme_applies_when_logged_in(world):
    c = client_for("member_a@test.org")
    set_theme(c, "dark", "/sites")
    assert 'data-theme="dark"' in c.get("/").text and 'data-theme="dark"' in c.get("/club").text


def test_week_title():
    assert week_title(date(2026, 11, 23)) == "Semaine du 23 au 29 novembre"
    assert week_title(date(2026, 11, 30)) == "Semaine du 30 novembre au 6 décembre"


def test_agenda_week_navigation_and_content(world, db):
    admin = client_for("admin_a@test.org")
    day = date.today() + timedelta(days=9)
    admin.post("/outings/new", data={"title": "Bloc du club", "site_id": 1, "day": day.isoformat(), "csrf": csrf(admin, "/outings/new")})
    page = admin.get("/").text
    assert "Bloc du club" in page and "À venir" in page and "Fontainebleau" in page
    monday = day - timedelta(days=day.weekday())
    week = admin.get(f"/?week={day.isoformat()}").text
    assert f"/outings/{db.query(Outing).one().id}" in week and 'class="chip' in week
    assert "Aujourd'hui" in week                                              # autre semaine : bouton retour
    assert admin.get("/?week=n-importe-quoi").status_code == 200             # paramètre invalide : semaine courante
    assert f"week={(monday - timedelta(days=7)).isoformat()}" in week and f"week={(monday + timedelta(days=7)).isoformat()}" in week


def test_agenda_isolation_between_clubs(world, db):
    admin = client_for("admin_a@test.org")
    admin.post("/outings/new", data={"title": "Secret du club A", "site_id": 1,
                                     "day": (date.today() + timedelta(days=2)).isoformat(), "csrf": csrf(admin, "/outings/new")})
    assert "Secret du club A" not in client_for("admin_b@test.org").get("/").text
    root = client_for("root@test.org", "rootpassword123").get("/").text
    assert "Secret du club A" in root and "Club A" in root and "Ma réponse" not in root   # le super admin ne répond pas


def test_inline_reply_from_agenda(world, db):
    admin = client_for("admin_a@test.org")
    day = (date.today() + timedelta(days=3)).isoformat()
    admin.post("/outings/new", data={"title": "Sortie", "site_id": 1, "day": day, "csrf": csrf(admin, "/outings/new")})
    oid = db.query(Outing).one().id
    m = client_for("member_a@test.org")
    page = m.get("/").text
    assert "Pas encore répondu" in page and "À répondre" in page
    r = m.post(f"/outings/{oid}/respond", data={"status": "yes", "seats": "3", "next": f"/?week={day}", "csrf": csrf(m)},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/?week={day}"
    p = db.query(Participation).one()
    assert (p.status, p.seats) == ("yes", 3)
    page = m.get("/").text
    assert 'value="yes" aria-pressed="true"' in page and "Pas encore répondu" not in page and '<span class="stat">3</span>' in page and "place(s) proposée(s) par 1 conducteur(s)" in page
    r = m.post(f"/outings/{oid}/respond", data={"status": "no", "next": "https://evil.example", "csrf": csrf(m)}, follow_redirects=False)
    assert r.headers["location"] == f"/outings/{oid}"                          # redirection externe refusée


def test_club_hub(world, db):
    m = client_for("member_a@test.org").get("/club").text
    assert "Mon profil" in m and "Administration du club" not in m and "/admin/integrations" not in m
    a = client_for("admin_a@test.org").get("/club").text
    assert "Administration du club" in a and "/admin/logs" in a and "Super administration" not in a
    assert "Super administration" in client_for("root@test.org", "rootpassword123").get("/club").text


def test_admin_todo_counts_pending_invites(world, db, outbox):
    from .conftest import enable_mailjet
    enable_mailjet(db, world["a"].id)
    admin = client_for("admin_a@test.org")
    admin.post("/admin/users", data={"first_name": "Neo", "last_name": "T", "email": "neo@test.org", "role": "member",
                                     "invite": "1", "csrf": csrf(admin, "/admin/users")})
    assert db.query(AuthToken).filter_by(purpose="invite").count() == 1
    assert "Invitations en attente</span><strong>1</strong>" in admin.get("/").text
    assert "Pour le club" not in client_for("member_a@test.org").get("/").text


def test_error_page_renders_with_header(world):
    """Régression : l'en-tête lisait user.club après fermeture de la session (erreur 500 au lieu de 403)."""
    r = client_for("member_a@test.org").get("/outings/new")
    assert r.status_code == 403 and "Club A" in r.text and "Accès refusé" in r.text


def test_navigation_marks_current_page(world):
    c = client_for("admin_a@test.org")
    assert '<a href="/sites/favorites" aria-current="page">Favoris</a>' in c.get("/sites/favorites").text
    assert '<a href="/" aria-current="page">Agenda</a>' in c.get("/").text
    assert 'class="tabbar"' in c.get("/").text and 'class="tabbar"' not in TestClient(app).get("/login").text
