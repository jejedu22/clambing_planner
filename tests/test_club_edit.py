from datetime import date, timedelta

from app.models import AuditLog, Club, Outing
from .conftest import client_for, csrf


def edit(c, cid, name):
    return c.post(f"/admin/clubs/{cid}/edit", data={"name": name, "csrf": csrf(c, "/admin/clubs")}, follow_redirects=False)


def test_super_admin_renames_club(world, db):
    root = client_for("root@test.org", "rootpassword123")
    page = root.get(f"/admin/clubs/{world['a'].id}/edit").text
    assert 'value="Club A"' in page and "admin_a" in page and "Membres" in page
    assert edit(root, world["a"].id, "  Grimpeurs   de Paris ").status_code == 303
    db.expire_all()
    assert db.get(Club, world["a"].id).name == "Grimpeurs de Paris"
    ev = db.query(AuditLog).filter_by(event="club.renamed").one()
    assert ev.detail == "Club A -> Grimpeurs de Paris" and ev.club_id == world["a"].id
    assert "Grimpeurs de Paris" in client_for("member_a@test.org").get("/").text       # visible par les membres


def test_rename_validation(world, db):
    root = client_for("root@test.org", "rootpassword123")
    r = edit(root, world["a"].id, "club b")                                  # déjà pris (casse ignorée)
    assert r.status_code == 400 and "Un autre club porte déjà ce nom" in r.text and 'value="club b"' in r.text
    assert edit(root, world["a"].id, "x").status_code == 400
    assert edit(root, world["a"].id, "y" * 121).status_code == 400
    assert edit(root, world["a"].id, "CLUB A").status_code == 303            # changer seulement la casse : autorisé
    assert edit(root, 999, "Nouveau").status_code == 404
    db.expire_all()
    assert db.get(Club, world["a"].id).name == "CLUB A" and db.get(Club, world["b"].id).name == "Club B"


def test_only_super_admin_can_edit(world, db):
    for email in ("admin_a@test.org", "member_a@test.org"):
        c = client_for(email)
        assert c.get(f"/admin/clubs/{world['a'].id}/edit").status_code == 403
        assert edit(c, world["a"].id, "Piraté").status_code == 403
    db.expire_all()
    assert db.get(Club, world["a"].id).name == "Club A"


def test_summary_and_links(world, db):
    admin = client_for("admin_a@test.org")
    admin.post("/outings/new", data={"title": "S", "site_id": 1, "day": (date.today() + timedelta(days=3)).isoformat(),
                                     "csrf": csrf(admin, "/outings/new")})
    root = client_for("root@test.org", "rootpassword123")
    page = root.get(f"/admin/clubs/{world['a'].id}/edit").text
    assert "1 dont 1 à venir" in page and "Suppression impossible" in page
    for link in ("/admin/users?club=", "/admin/club-location?club=", "/admin/integrations?club=", "/admin/logs?club="):
        assert f"{link}{world['a'].id}" in page
    empty = Club(name="Vide"); db.add(empty); db.commit()
    page = root.get(f"/admin/clubs/{empty.id}/edit").text
    assert "Supprimer le club" in page and "Aucun" in page                  # aucun administrateur signalé
    assert f"/admin/clubs/{world['a'].id}/edit" in root.get("/admin/clubs").text


def test_create_rejects_duplicate_names_case_insensitive(world, db):
    root = client_for("root@test.org", "rootpassword123")
    root.post("/admin/clubs", data={"name": "  club   a ", "csrf": csrf(root, "/admin/clubs")})
    assert db.query(Club).count() == 2
    root.post("/admin/clubs", data={"name": "Nouveau  club", "csrf": csrf(root, "/admin/clubs")})
    assert db.query(Club).filter_by(name="Nouveau club").count() == 1
