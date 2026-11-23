from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import TYPE_LABELS, Club, ClubFavorite, Site, User
from ..security import login_required
from ..services import weather
from ..web import render
from .favorites import favorite_ids, map_view

router = APIRouter()
PAGE = 30


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filtered(q: str, outdoor: bool, kind: str, fav_club: int | None = None):
    stmt = select(Site)
    if fav_club is not None:
        stmt = stmt.where(Site.id.in_(select(ClubFavorite.site_id).where(ClubFavorite.club_id == fav_club)))
    if q:
        stmt = stmt.where(Site.name.ilike(_like(q), escape="\\"))
    if outdoor:
        stmt = stmt.where(Site.outdoor.is_(True))
    if kind in TYPE_LABELS:
        stmt = stmt.where(Site.types.contains(kind))
    return stmt


@router.get("/sites")
def sites_page(request: Request, q: str = "", kind: str = "", outdoor: int = 1, page: int = 1, fav: int = 0,
               user: User = Depends(login_required), db: Session = Depends(get_db)):
    q = q.strip()[:100]
    page = max(page, 1)
    fav_only = bool(fav) and user.club_id is not None
    stmt = _filtered(q, bool(outdoor), kind, user.club_id if fav_only else None)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(
        stmt.order_by(Site.name.is_(None), Site.name).offset((page - 1) * PAGE).limit(PAGE)
    ).all()
    fav_ids = {r for r in db.scalars(select(ClubFavorite.site_id).where(ClubFavorite.club_id == user.club_id))} \
        if user.club_id else set()
    return render(request, "sites.html", rows=rows, total=total, q=q, kind=kind, fav=int(fav_only), fav_ids=fav_ids,
                  view=map_view(db, user.club),
                  outdoor=outdoor, page=page, pages=max(1, -(-total // PAGE)), type_labels=TYPE_LABELS)


@router.get("/api/sites")
def sites_api(q: str = "", bbox: str = "", kind: str = "", outdoor: int = 1, fav: int = 0,
              limit: int = Query(300, ge=1, le=500),
              user: User = Depends(login_required), db: Session = Depends(get_db)):
    """Sites au format JSON : par zone visible (bbox=sud,ouest,nord,est) ou par recherche de nom."""
    if fav and user.club_id is None:
        return {"truncated": False, "sites": []}        # pas de club : pas de favoris
    stmt = _filtered(q.strip()[:100], bool(outdoor), kind, user.club_id if fav else None)
    if bbox:
        try:
            s, w, n, e = (float(v) for v in bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox invalide")
        stmt = stmt.where(Site.lat.between(s, n), Site.lon.between(w, e))
    rows = db.scalars(stmt.order_by(Site.name.is_(None), Site.name).limit(limit + 1)).all()
    ids = favorite_ids(db, user.club_id)
    return {
        "truncated": len(rows) > limit,
        "sites": [{"id": r.id, "name": r.display_name, "lat": r.lat, "lon": r.lon,
                   "types": r.type_labels, "fav": r.id in ids} for r in rows[:limit]],
    }


@router.get("/sites/{site_id}")
def site_detail(site_id: int, request: Request, user: User = Depends(login_required),
                db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404)
    fav = db.get(ClubFavorite, (user.club_id, site.id)) if user.club_id else None
    fav_clubs, clubs = [], []
    if user.is_super:   # le super administrateur voit et gère les favoris de tous les clubs
        clubs = db.scalars(select(Club).order_by(Club.name)).all()
        have = set(db.scalars(select(ClubFavorite.club_id).where(ClubFavorite.site_id == site.id)))
        fav_clubs = [c for c in clubs if c.id in have]
    return render(request, "site_detail.html", site=site, fav=fav, fav_clubs=fav_clubs, clubs=clubs,
                  forecast=weather.get_forecast(site.lat, site.lon))
