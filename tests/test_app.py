from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.models import Outing, Participation, Role, User
from app.services import weather
from app.services.sites_import import parse_element
from .conftest import client_for, csrf, make_user

FUTURE = (date.today() + timedelta(days=5)).isoformat()


def new_outing(c, club_id=None, site_id=1, title="Sortie bloc"):
    data = {"title": title, "site_id": site_id, "day": FUTURE, "csrf": csrf(c, "/outings/new")}
    if club_id:
        data["club_id"] = club_id
    return c.post("/outings/new", data=data, follow_redirects=False)


# --- authentification / CSRF -------------------------------------------------

def test_requires_login(world):
    r = TestClient(app).get("/outings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_post_without_csrf_rejected(world):
    r = TestClient(app).post("/login", data={"email": "admin_a@test.org", "password": "x"})
    assert r.status_code == 403


def test_bad_password_and_lockout(world):
    c = TestClient(app)
    token = csrf(c, "/login")
    for _ in range(5):
        r = c.post("/login", data={"email": "member_a@test.org", "password": "nope", "csrf": token})
        assert r.status_code == 401
    r = c.post("/login", data={"email": "member_a@test.org", "password": "password12345", "csrf": token})
    assert r.status_code == 429


def test_inactive_user_cannot_login(world, db):
    u = db.query(User).filter_by(email="member_a@test.org").one()
    u.is_active = False; db.commit()
    c = TestClient(app)
    r = c.post("/login", data={"email": u.email, "password": "password12345", "csrf": csrf(c, "/login")})
    assert r.status_code == 401


def test_must_change_password(world, db):
    u = db.query(User).filter_by(email="member_a@test.org").one()
    u.must_change_password = True; db.commit()
    c = client_for(u.email)
    assert c.get("/outings", follow_redirects=False).headers["location"] == "/password"
    r = c.post("/password", data={"current": "password12345", "new": "a-brand-new-pass", "confirm": "a-brand-new-pass",
                                  "csrf": csrf(c, "/password")}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/outings").status_code == 200


# --- rôles -------------------------------------------------------------------

def test_member_cannot_create_outing_or_manage_users(world):
    c = client_for("member_a@test.org")
    assert c.get("/outings/new").status_code == 403
    assert c.get("/admin/users").status_code == 403
    assert c.get("/admin/clubs").status_code == 403
    assert c.get("/admin/sites").status_code == 403


def test_club_admin_creates_outing_in_own_club_only(world, db):
    c = client_for("admin_a@test.org")
    # un club_id forgé dans le formulaire est ignoré
    r = new_outing(c, club_id=world["b"].id)
    assert r.status_code == 303
    assert db.query(Outing).one().club_id == world["a"].id


def test_outings_isolated_between_clubs(world, db):
    new_outing(client_for("admin_a@test.org"))
    oid = db.query(Outing).one().id
    other = client_for("admin_b@test.org")
    assert other.get(f"/outings/{oid}").status_code == 404
    assert "Sortie bloc" not in other.get("/outings").text
    assert other.post(f"/outings/{oid}/respond", data={"status": "yes", "csrf": csrf(other)}).status_code == 404
    assert other.post(f"/outings/{oid}/delete", data={"csrf": csrf(other)}).status_code == 404
    assert client_for("member_a@test.org").get(f"/outings/{oid}").status_code == 200


def test_super_admin_sees_everything(world, db):
    new_outing(client_for("admin_a@test.org"))
    root = client_for("root@test.org", "rootpassword123")
    assert "Sortie bloc" in root.get("/outings").text
    assert root.get("/admin/clubs").status_code == 200
    assert root.get("/admin/sites").status_code == 200
    oid = db.query(Outing).one().id
    assert root.get(f"/outings/{oid}").status_code == 200


def test_club_admin_user_management_is_scoped(world, db):
    c = client_for("admin_a@test.org")
    r = c.post("/admin/users", data={"name": "Nouveau", "email": "new@test.org", "role": "member",
                                     "club_id": world["b"].id, "csrf": csrf(c, "/admin/users")})
    assert r.status_code == 200 and "Mot de passe provisoire" in r.text
    assert db.query(User).filter_by(email="new@test.org").one().club_id == world["a"].id
    # impossible de créer un super administrateur
    r = c.post("/admin/users", data={"name": "X", "email": "x@test.org", "role": "super_admin",
                                     "csrf": csrf(c, "/admin/users")})
    assert r.status_code == 400
    # impossible de modifier un utilisateur d'un autre club
    other = db.query(User).filter_by(email="admin_b@test.org").one()
    r = c.post(f"/admin/users/{other.id}", data={"role": "member", "csrf": csrf(c, "/admin/users")})
    assert r.status_code == 404
    # ni le super administrateur
    root = db.query(User).filter_by(role=Role.SUPER_ADMIN.value).one()
    assert c.post(f"/admin/users/{root.id}", data={"role": "member", "csrf": csrf(c, "/admin/users")}).status_code == 404


def test_super_admin_creates_club_and_admin(world, db):
    root = client_for("root@test.org", "rootpassword123")
    root.post("/admin/clubs", data={"name": "Club C", "csrf": csrf(root, "/admin/clubs")})
    from app.models import Club
    club = db.query(Club).filter_by(name="Club C").one()
    r = root.post("/admin/users", data={"name": "Boss C", "email": "boss@test.org", "role": "club_admin",
                                        "club_id": club.id, "csrf": csrf(root, "/admin/users")})
    assert r.status_code == 200
    u = db.query(User).filter_by(email="boss@test.org").one()
    assert u.club_id == club.id and u.is_admin and u.must_change_password


# --- participation -----------------------------------------------------------

def test_participation_and_capacity(world, db):
    c = client_for("admin_a@test.org")
    c.post("/outings/new", data={"title": "T", "site_id": 1, "day": FUTURE, "max_participants": 1,
                                 "csrf": csrf(c, "/outings/new")})
    oid = db.query(Outing).one().id
    m = client_for("member_a@test.org")
    m.post(f"/outings/{oid}/respond", data={"status": "yes", "seats": 3, "csrf": csrf(m)})
    p = db.query(Participation).one()
    assert (p.status, p.seats) == ("yes", 3)
    # le club admin ne peut plus s'inscrire : complet
    c.post(f"/outings/{oid}/respond", data={"status": "yes", "csrf": csrf(c)})
    assert db.query(Participation).count() == 1
    # changer d'avis fonctionne et libère la place
    m.post(f"/outings/{oid}/respond", data={"status": "no", "csrf": csrf(m)})
    db.expire_all()
    p = db.query(Participation).one()
    assert (p.status, p.seats) == ("no", 0)


def test_sites_api_requires_login_and_filters(world):
    assert TestClient(app).get("/api/sites", follow_redirects=False).status_code == 303
    c = client_for("member_a@test.org")
    data = c.get("/api/sites", params={"bbox": "48,2,49,3"}).json()
    assert [s["name"] for s in data["sites"]] == ["Fontainebleau"]
    assert c.get("/api/sites", params={"bbox": "0,0,1,1"}).json()["sites"] == []
    assert c.get("/api/sites", params={"bbox": "bad"}).status_code == 400
    assert c.get("/api/sites", params={"q": "100%"}).json()["sites"] == []


# --- services ----------------------------------------------------------------

def test_parse_osm_elements():
    el = {"type": "way", "id": 42, "center": {"lat": 44.1, "lon": 5.2},
          "tags": {"sport": "climbing", "name": "Buoux", "climbing:sport": "yes",
                   "climbing:multipitch": "yes", "climbing:boulder": "no", "climbing:routes": "300",
                   "website": "javascript:alert(1)"}}
    d = parse_element(el)
    assert d["osm_id"] == "way/42" and d["types"] == "sport,multipitch" and d["routes"] == 300
    assert d["outdoor"] is True and d["website"] is None
    gym = parse_element({"type": "node", "id": 1, "lat": 1, "lon": 2,
                         "tags": {"sport": "climbing", "leisure": "sports_centre"}})
    assert gym["outdoor"] is False and gym["name"] is None
    assert parse_element({"type": "relation", "id": 3, "tags": {}}) is None


def test_weather_verdict_and_parse():
    payload = {"daily": {
        "time": ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05", "2026-06-06"],
        "weather_code": [63, 3, 0, 0, 0, 95],
        "temperature_2m_max": [15, 18, 22, 23, 24, 25], "temperature_2m_min": [8, 9, 10, 11, 12, 12],
        "precipitation_sum": [12.0, 0.0, 0.0, 0.0, 0.0, 9.0],
        "precipitation_probability_max": [90, 20, 5, 5, 5, 90],
        "wind_speed_10m_max": [20, 15, 10, 10, 10, 60]}}
    days = weather.parse_daily(payload)
    assert days[0]["verdict"] == "bad"
    assert days[1]["verdict"] == "warn" and "humide" in days[1]["verdict_text"]   # pluie la veille
    assert days[2]["verdict"] == "warn"   # encore humide 2 jours après
    assert days[4]["verdict"] == "ok"
    assert days[5]["verdict"] == "bad" and days[5]["icon"] == "⛈️"


# --- rendu de toutes les pages (météo simulée) --------------------------------

def test_all_pages_render(world, db, monkeypatch):
    payload = {"daily": {
        "time": [(date.today() + timedelta(days=i - 3)).isoformat() for i in range(19)],
        "weather_code": [1] * 19, "temperature_2m_max": [20] * 19, "temperature_2m_min": [10] * 19,
        "precipitation_sum": [0.0] * 19, "precipitation_probability_max": [10] * 19,
        "wind_speed_10m_max": [12] * 19}}
    monkeypatch.setattr(weather, "get_forecast", lambda lat, lon: weather.parse_daily(payload))
    monkeypatch.setattr(weather, "forecast_for_day",
                        lambda lat, lon, d: next((x for x in weather.parse_daily(payload) if x["date"] == d), None))
    admin = client_for("admin_a@test.org")
    new_outing(admin)
    oid = db.query(Outing).one().id
    for path in ["/", "/outings", "/outings?past=1", "/outings/new", "/outings/new?site=1",
                 f"/outings/{oid}", f"/outings/{oid}/edit", "/sites", "/sites?q=font&kind=boulder",
                 "/sites/1", "/admin/users", "/password"]:
        r = admin.get(path)
        assert r.status_code == 200, path
    assert "Bonnes conditions" in admin.get(f"/outings/{oid}").text
    assert "Fontainebleau" in admin.get("/").text
    assert admin.get("/sites/999").status_code == 404
    root = client_for("root@test.org", "rootpassword123")
    for path in ["/", "/outings", "/outings/new", "/admin/clubs", "/admin/users", "/admin/sites"]:
        assert root.get(path).status_code == 200, path
    # modification + annulation
    r = admin.post(f"/outings/{oid}/edit", data={"title": "Renommée", "site_id": 1, "day": FUTURE,
                   "csrf": csrf(admin)}, follow_redirects=False)
    assert r.status_code == 303 and "Renommée" in admin.get(f"/outings/{oid}").text
    admin.post(f"/outings/{oid}/cancel", data={"csrf": csrf(admin)})
    assert "Annulée" in admin.get(f"/outings/{oid}").text


def test_xss_is_escaped(world, db):
    c = client_for("admin_a@test.org")
    c.post("/outings/new", data={"title": "<script>alert(1)</script>", "site_id": 1, "day": FUTURE,
                                 "csrf": csrf(c, "/outings/new")})
    oid = db.query(Outing).one().id
    assert "<script>alert(1)</script>" not in c.get(f"/outings/{oid}").text


# --- PWA ----------------------------------------------------------------------

def test_pwa_files_are_public_and_valid(world):
    c = TestClient(app)
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    data = m.json()
    assert data["display"] == "standalone" and data["start_url"] == "/"
    sizes = {i["sizes"] for i in data["icons"]}
    assert {"192x192", "512x512"} <= sizes and any(i["purpose"] == "maskable" for i in data["icons"])
    for icon in data["icons"]:                       # toutes les icônes déclarées existent
        assert c.get(icon["src"]).status_code == 200
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert sw.headers["service-worker-allowed"] == "/" and sw.headers["cache-control"] == "no-cache"
    assert "__VERSION__" not in sw.text
    off = c.get("/offline")
    assert off.status_code == 200 and "Hors connexion" in off.text
    assert 'rel="manifest"' in c.get("/login").text and "/static/pwa.js" in c.get("/login").text


def test_sw_precache_assets_exist(world):
    import re
    c = TestClient(app)
    sw = c.get("/sw.js").text
    block = re.search(r"PRECACHE = \[(.*?)\];", sw, re.S).group(1)
    for path in re.findall(r"'(/[^']+)'", block):
        assert c.get(path).status_code == 200, path
