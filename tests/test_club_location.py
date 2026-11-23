import html
import json
import re

from sqlalchemy import create_engine, inspect, text

from app.db import Base
from app.migrations import upgrade_schema
from app.models import AuditLog, Club, ClubFavorite, Site
from .conftest import client_for, csrf


def save(c, **data):
    return c.post("/admin/club-location", data={"csrf": csrf(c, "/admin/club-location"), **data}, follow_redirects=False)


def club(db, cid):
    db.expire_all()
    return db.get(Club, cid)


def view_of(page):
    raw = re.search(r"data-view='([^']*)'", page).group(1)
    return json.loads(html.unescape(raw))


def test_admin_sets_and_clears_location(world, db):
    a = client_for("admin_a@test.org")
    assert a.get("/admin/club-location").status_code == 200
    assert save(a, lat="48.5401", lon="2.6589", label="  Gymnase   de Melun ").status_code == 303
    c = club(db, world["a"].id)
    assert (c.home_lat, c.home_lon, c.home_label) == (48.5401, 2.6589, "Gymnase de Melun")
    save(a, lat="48,6", lon="2,7")                                             # virgule décimale acceptée
    assert club(db, world["a"].id).home_lat == 48.6 and club(db, world["a"].id).home_label is None
    save(a, clear="1")
    assert not club(db, world["a"].id).has_home
    assert [e.event for e in db.query(AuditLog).filter(AuditLog.event.like("club.location%")).order_by(AuditLog.id)] == \
        ["club.location_set", "club.location_set", "club.location_cleared"]


def test_invalid_locations_rejected(world, db):
    a = client_for("admin_a@test.org")
    for lat, lon in (("", ""), ("abc", "2"), ("91", "2"), ("48", "181"), ("nan", "2"), ("inf", "2")):
        save(a, lat=lat, lon=lon)
        assert not club(db, world["a"].id).has_home, (lat, lon)
    save(a, lat="48", lon="2", label="x" * 81)
    assert not club(db, world["a"].id).has_home


def test_permissions_and_isolation(world, db):
    m = client_for("member_a@test.org")
    assert m.get("/admin/club-location").status_code == 403
    assert m.post("/admin/club-location", data={"lat": "1", "lon": "1", "csrf": csrf(m)}).status_code == 403
    a = client_for("admin_a@test.org")
    save(a, club_id=str(world["b"].id), lat="45", lon="5")                     # club_id forgé : ignoré
    assert club(db, world["a"].id).home_lat == 45 and not club(db, world["b"].id).has_home
    root = client_for("root@test.org", "rootpassword123")
    assert save(root, lat="45", lon="5").status_code == 400                    # doit choisir un club
    save(root, club_id=str(world["b"].id), lat="44", lon="6")
    assert club(db, world["b"].id).home_lat == 44
    assert "Club B" in root.get(f"/admin/club-location?club={world['b'].id}").text


def test_sites_map_is_framed_on_home_and_favorites(world, db):
    db.add_all([Site(osm_id="node/2", name="Buoux", lat=43.8, lon=5.4), Site(osm_id="node/3", name="Céüse", lat=44.5, lon=5.93)])
    db.commit()
    db.add_all([ClubFavorite(club_id=world["a"].id, site_id=2), ClubFavorite(club_id=world["b"].id, site_id=3)]); db.commit()
    a = client_for("admin_a@test.org")
    v = view_of(a.get("/sites").text)
    assert v == {"home": None, "fit": [[43.8, 5.4]]}                          # favoris seuls ; jamais ceux du club B
    assert "indiquez la localisation du club" in a.get("/sites").text
    save(a, lat="48.4", lon="2.6", label="Local")
    v = view_of(client_for("member_a@test.org").get("/sites").text)           # les membres en profitent aussi
    assert v["home"] == {"lat": 48.4, "lon": 2.6, "label": "Local"} and sorted(v["fit"]) == [[43.8, 5.4], [48.4, 2.6]]
    assert "indiquez la localisation du club" not in a.get("/sites").text
    assert view_of(client_for("root@test.org", "rootpassword123").get("/sites").text) == {"home": None, "fit": []}


def test_home_label_cannot_break_out_of_the_page(world, db):
    a = client_for("admin_a@test.org")
    evil = "'></div><script>alert(1)</script>"
    save(a, lat="48", lon="2", label=evil)
    page = a.get("/sites").text
    assert "<script>alert(1)</script>" not in page and view_of(page)["home"]["label"] == evil
    assert "<script>alert(1)</script>" not in a.get("/club").text


def test_schema_upgrade_adds_club_location(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO clubs (name, created_at) VALUES ('Ancien', '2026-01-01')"))
        for col in ("home_lat", "home_lon", "home_label"):
            c.execute(text(f"ALTER TABLE clubs DROP COLUMN {col}"))
    upgrade_schema(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("clubs")}
    assert {"home_lat", "home_lon", "home_label"} <= cols
    with engine.connect() as c:
        assert c.execute(text("SELECT name, home_lat FROM clubs")).one() == ("Ancien", None)
