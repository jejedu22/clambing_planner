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
