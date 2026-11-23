import json

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditLog, Club, ClubFavorite, ClubIntegration, Membership, Role, Site
from app.routes import favorites
from .conftest import client_for, csrf, enable_helloasso, enable_mailjet, make_user


def add(c, site_id=1, **extra):
    return c.post(f"/sites/{site_id}/favorite", data={"csrf": csrf(c, "/sites"), **extra}, follow_redirects=False)


def remove(c, site_id=1, **extra):
    return c.post(f"/sites/{site_id}/unfavorite", data={"csrf": csrf(c, "/sites"), **extra}, follow_redirects=False)


def favs(db, club_id):
    db.expire_all()
    return {f.site_id: f.note for f in db.query(ClubFavorite).filter_by(club_id=club_id)}


def test_admin_adds_updates_and_removes(world, db):
    a = client_for("admin_a@test.org")
    assert add(a, note="  Parking   à 5 min  ").status_code == 303
    assert favs(db, world["a"].id) == {1: "Parking à 5 min"}                 # espaces normalisés
    add(a, note="Nouvelle note")                                              # idempotent : mise à jour
    assert favs(db, world["a"].id) == {1: "Nouvelle note"} and db.query(ClubFavorite).count() == 1
    add(a, note="")
    assert favs(db, world["a"].id) == {1: None}
    assert remove(a).status_code == 303 and favs(db, world["a"].id) == {}
    assert remove(a).status_code == 303                                       # retirer deux fois : sans erreur
    assert [e.event for e in db.query(AuditLog).filter(AuditLog.event.like("favorite.%")).order_by(AuditLog.id)] == \
        ["favorite.added", "favorite.updated", "favorite.updated", "favorite.removed"]


def test_members_see_but_cannot_manage(world, db):
    a = client_for("admin_a@test.org"); add(a, note="Accès par le nord")
    m = client_for("member_a@test.org")
    assert add(m).status_code == 403 and remove(m).status_code == 403
    assert favs(db, world["a"].id) == {1: "Accès par le nord"}                # intact
    page = m.get("/sites/favorites").text
    assert "Fontainebleau" in page and "Accès par le nord" in page and "Retirer" not in page and "Organiser une sortie" not in page
    detail = m.get("/sites/1").text
    assert "Accès par le nord" in detail and "Ajouter aux favoris" not in detail and "Retirer des favoris" not in detail
    assert "Retirer des favoris" in a.get("/sites/1").text
    assert TestClient(app).get("/sites/favorites", follow_redirects=False).status_code == 303


def test_clubs_are_isolated(world, db):
    db.add(Site(osm_id="node/2", name="Buoux secret", lat=43.8, lon=5.4, types="sport")); db.commit()
    client_for("admin_a@test.org")
    a, b = client_for("admin_a@test.org"), client_for("admin_b@test.org")
    add(a, 1, note="NOTE-PRIVEE-A"); add(b, 2, note="NOTE-PRIVEE-B")
    pa, pb = a.get("/sites/favorites").text, b.get("/sites/favorites").text
    assert "Fontainebleau" in pa and "Buoux secret" not in pa and "NOTE-PRIVEE-B" not in pa
    assert "Buoux secret" in pb and "Fontainebleau" not in pb and "NOTE-PRIVEE-A" not in pb
    assert "NOTE-PRIVEE-B" not in a.get("/sites/2").text and "NOTE-PRIVEE-A" not in b.get("/sites/1").text
    # ?club= et club_id forgés sont ignorés pour un non super-administrateur
    assert "NOTE-PRIVEE-B" not in a.get(f"/sites/favorites?club={world['b'].id}").text
    add(a, 2, club_id=str(world["b"].id)); remove(a, 2, club_id=str(world["b"].id))
    # ... le club B est intact ; l'ajout puis le retrait sont retombés sur le club de l'administrateur (A)
    assert favs(db, world["b"].id) == {2: "NOTE-PRIVEE-B"} and favs(db, world["a"].id) == {1: "NOTE-PRIVEE-A"}
    # API : le drapeau et le filtre sont propres au club de l'utilisateur
    mem_b = make_user(db, "mem_b@test.org", Role.MEMBER.value, world["b"].id)
    cb = client_for("mem_b@test.org")
    data = {s["id"]: s["fav"] for s in cb.get("/api/sites").json()["sites"]}
    assert data == {1: False, 2: True}
    assert [s["id"] for s in cb.get("/api/sites?fav=1").json()["sites"]] == [2]
    assert [s["id"] for s in client_for("member_a@test.org").get("/api/sites?fav=1").json()["sites"]] == [1]


def test_sites_page_filter_and_star(world, db):
    db.add(Site(osm_id="node/2", name="Autre site", lat=43.8, lon=5.4, types="sport")); db.commit()
    a = client_for("admin_a@test.org"); add(a)
    page = a.get("/sites").text
    assert "⭐" in page and "★ Retirer" in page and "☆ Favori" in page and 'name="next"' in page
    only = a.get("/sites?fav=1").text
    assert "Fontainebleau" in only and "Autre site" not in only and "(1)" in only
    assert "fav=1" in a.get("/sites?fav=1&q=").text
    m = client_for("member_a@test.org").get("/sites").text
    assert "⭐" in m and "☆ Favori" not in m and "★ Retirer" not in m                 # un membre ne gère pas


def test_super_admin_manages_any_club(world, db):
    root = client_for("root@test.org", "rootpassword123")
    assert add(root).status_code == 400                                        # il doit choisir un club
    assert add(root, club_id=str(world["b"].id), note="Choix du super").status_code == 303
    assert favs(db, world["b"].id) == {1: "Choix du super"} and favs(db, world["a"].id) == {}
    detail = root.get("/sites/1").text
    assert "Favoris des clubs" in detail and "Club B" in detail
    assert "Choix du super" in root.get(f"/sites/favorites?club={world['b'].id}").text
    assert "Choisissez un club" in root.get("/sites/favorites").text
    assert remove(root, club_id=str(world["b"].id)).status_code == 303 and favs(db, world["b"].id) == {}
    assert add(root, club_id="999").status_code == 400


def test_validation_and_limits(world, db, monkeypatch):
    a = client_for("admin_a@test.org")
    assert add(a, 999).status_code == 404
    add(a, note="x" * 301)
    assert favs(db, world["a"].id) == {}                                       # note trop longue : refusée
    monkeypatch.setattr(favorites, "MAX_FAVORITES", 1)
    db.add(Site(osm_id="node/3", name="S3", lat=1, lon=1)); db.commit()
    add(a, 1); add(a, 2)
    assert set(favs(db, world["a"].id)) == {1}                                 # plafond atteint


def test_next_redirect_is_safe(world, db):
    a = client_for("admin_a@test.org")
    for evil in ("https://evil.example/x", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        r = add(a, next=evil)
        assert r.headers["location"] == "/sites/1", evil
    assert add(a, next="/sites?fav=1").headers["location"] == "/sites?fav=1"
    assert remove(a, next="/sites/favorites").headers["location"] == "/sites/favorites"


def test_notes_are_escaped(world, db):
    a = client_for("admin_a@test.org"); add(a, note="<script>alert(1)</script>")
    for path in ("/sites/favorites", "/sites/1"):
        assert "<script>alert(1)</script>" not in a.get(path).text


def test_favorites_picker_data_for_outing_form(world, db):
    a = client_for("admin_a@test.org"); add(a)
    assert 'id="site-search"' in a.get("/outings/new").text
    assert [s["name"] for s in a.get("/api/sites?fav=1&limit=15").json()["sites"]] == ["Fontainebleau"]
    root = client_for("root@test.org", "rootpassword123")
    assert root.get("/api/sites?fav=1").json()["sites"] == []                  # pas de club : pas de favoris


def test_club_deletion_cleans_related_rows(world, db):
    from app.models import Club
    c = Club(name="Éphémère"); db.add(c); db.commit(); cid = c.id
    db.add(ClubFavorite(club_id=c.id, site_id=1, note="x")); db.add(Membership(club_id=c.id, ha_item_id="1", email="a@b.org", state="Processed"))
    enable_mailjet(db, c.id); enable_helloasso(db, c.id); db.commit()
    root = client_for("root@test.org", "rootpassword123")
    root.post(f"/admin/clubs/{cid}/delete", data={"csrf": csrf(root, "/admin/clubs")})
    db.expire_all()
    assert db.query(Club).filter_by(id=cid).count() == 0
    assert db.query(ClubFavorite).filter_by(club_id=cid).count() == db.query(Membership).filter_by(club_id=cid).count() == 0
    assert db.query(ClubIntegration).filter_by(club_id=cid).count() == 0
