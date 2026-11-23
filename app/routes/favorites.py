"""Sites favoris par club : gérés par les administrateurs, consultables par les membres."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..audit import audit
from ..db import get_db
from ..models import Club, ClubFavorite, Site, User
from ..security import admin_required, login_required
from ..web import flash, render

router = APIRouter()
MAX_FAVORITES = 500
MAX_NOTE = 300


def safe_next(url: str, default: str) -> str:
    """Redirection interne uniquement (pas de //hote ni de schéma : pas de redirection ouverte)."""
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url and "\n" not in url else default


def managed_club(user: User, db: Session, club_id: str) -> Club:
    """Club dont `user` gère les favoris : le sien ; le super administrateur choisit."""
    cid = int(club_id) if user.is_super and club_id.isdigit() else user.club_id
    club = db.get(Club, cid) if cid else None
    if club is None:
        raise HTTPException(400, "Choisissez un club.")
    return club


def favorite_ids(db: Session, club_id: int | None) -> set[int]:
    if club_id is None:
        return set()
    return set(db.scalars(select(ClubFavorite.site_id).where(ClubFavorite.club_id == club_id)))


@router.get("/sites/favorites")
def favorites_page(request: Request, club: int | None = None, user: User = Depends(login_required),
                   db: Session = Depends(get_db)):
    cid = club if user.is_super else user.club_id          # un membre ne choisit jamais le club
    current = db.get(Club, cid) if cid else None
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    rows = []
    if current:
        rows = db.scalars(select(ClubFavorite).options(joinedload(ClubFavorite.site))
                          .where(ClubFavorite.club_id == current.id)
                          .join(Site).order_by(func.lower(func.coalesce(Site.name, "~")))).all()
    return render(request, "favorites.html", club=current, clubs=clubs, rows=rows,
                  can_manage=user.is_admin and current is not None)


@router.post("/sites/{site_id}/favorite")
def favorite_add(site_id: int, request: Request, club_id: str = Form(""), note: str = Form(""),
                 next: str = Form(""), user: User = Depends(admin_required), db: Session = Depends(get_db)):
    club = managed_club(user, db, club_id)
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404)
    note = " ".join(note.split())
    dest = safe_next(next, f"/sites/{site.id}")
    if len(note) > MAX_NOTE:
        flash(request, f"Note trop longue ({MAX_NOTE} caractères maximum).", "error")
        return RedirectResponse(dest, status_code=303)
    fav = db.get(ClubFavorite, (club.id, site.id))
    if fav is None:
        if db.scalar(select(func.count()).select_from(ClubFavorite).where(ClubFavorite.club_id == club.id)) >= MAX_FAVORITES:
            flash(request, f"Limite de {MAX_FAVORITES} favoris atteinte.", "error")
            return RedirectResponse(dest, status_code=303)
        db.add(ClubFavorite(club_id=club.id, site_id=site.id, note=note or None, added_by=user.id))
        event = "favorite.added"
    else:
        fav.note = note or None
        event = "favorite.updated"
    db.commit()
    audit(event, request, club_id=club.id, target=f"site:{site.id}")
    flash(request, "Site ajouté aux favoris." if event == "favorite.added" else "Note enregistrée.", "ok")
    return RedirectResponse(dest, status_code=303)


@router.post("/sites/{site_id}/unfavorite")
def favorite_remove(site_id: int, request: Request, club_id: str = Form(""), next: str = Form(""),
                    user: User = Depends(admin_required), db: Session = Depends(get_db)):
    club = managed_club(user, db, club_id)
    fav = db.get(ClubFavorite, (club.id, site_id))
    if fav is not None:
        db.delete(fav)
        db.commit()
        audit("favorite.removed", request, club_id=club.id, target=f"site:{site_id}")
        flash(request, "Site retiré des favoris.", "ok")
    return RedirectResponse(safe_next(next, f"/sites/{site_id}"), status_code=303)


# --- Localisation du club (centre de la carte) ----------------------------------

@router.get("/admin/club-location")
def club_location_form(request: Request, club: int | None = None, user: User = Depends(admin_required),
                       db: Session = Depends(get_db)):
    current = db.get(Club, club if user.is_super else user.club_id) if (club or user.club_id) else None
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    return render(request, "club_location.html", club=current, clubs=clubs)


@router.post("/admin/club-location")
def club_location_save(request: Request, club_id: str = Form(""), lat: str = Form(""), lon: str = Form(""),
                       label: str = Form(""), clear: str = Form(""), user: User = Depends(admin_required),
                       db: Session = Depends(get_db)):
    club = managed_club(user, db, club_id)
    back = f"/admin/club-location?club={club.id}" if user.is_super else "/admin/club-location"
    if clear:
        club.home_lat = club.home_lon = club.home_label = None
        db.commit()
        audit("club.location_cleared", request, club_id=club.id, target=f"club:{club.id}")
        flash(request, "Localisation supprimée : la carte s'ouvrira sur vos favoris ou sur la France.", "ok")
        return RedirectResponse(back, status_code=303)
    try:
        la, lo = float(lat.replace(",", ".")), float(lon.replace(",", "."))
        if not (-90 <= la <= 90 and -180 <= lo <= 180) or la != la or lo != lo:   # bornes + NaN
            raise ValueError
    except ValueError:
        flash(request, "Choisissez un point sur la carte.", "error")
        return RedirectResponse(back, status_code=303)
    label = " ".join(label.split())
    if len(label) > 80:
        flash(request, "Nom trop long (80 caractères maximum).", "error")
        return RedirectResponse(back, status_code=303)
    club.home_lat, club.home_lon, club.home_label = round(la, 6), round(lo, 6), label or None
    db.commit()
    audit("club.location_set", request, club_id=club.id, target=f"club:{club.id}")
    flash(request, "Localisation du club enregistrée.", "ok")
    return RedirectResponse(back, status_code=303)


def map_view(db: Session, club: Club | None) -> dict:
    """Données de cadrage initial de la carte : localisation du club + favoris."""
    if club is None:
        return {"home": None, "fit": []}
    fav = db.execute(select(Site.lat, Site.lon).join(ClubFavorite, ClubFavorite.site_id == Site.id)
                     .where(ClubFavorite.club_id == club.id)).all()
    home = {"lat": club.home_lat, "lon": club.home_lon, "label": club.home_label or club.name} if club.has_home else None
    points = [[round(la, 5), round(lo, 5)] for la, lo in fav]
    if home:
        points.append([home["lat"], home["lon"]])
    return {"home": home, "fit": points}
