"""Consultation du journal d'audit (super administrateur : tout ; administrateur de club : son club)."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, Club, User
from ..security import admin_required
from ..web import render

router = APIRouter(prefix="/admin")
PAGE = 50


@router.get("/logs")
def logs_page(request: Request, page: int = 1, event: str = "", club: int | None = None,
              user: User = Depends(admin_required), db: Session = Depends(get_db)):
    q = select(AuditLog)
    if user.is_super:
        if club:
            q = q.where(AuditLog.club_id == club)
    else:
        q = q.where(AuditLog.club_id == user.club_id)   # jamais les événements des autres clubs
    event = event.strip()[:60]
    if event:
        q = q.where(AuditLog.event.like(event.replace("%", "").replace("_", "\\_") + "%", escape="\\"))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    page = max(page, 1)
    rows = db.scalars(q.order_by(AuditLog.at.desc(), AuditLog.id.desc()).offset((page - 1) * PAGE).limit(PAGE)).all()
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    names = {c.id: c.name for c in db.scalars(select(Club))}
    return render(request, "admin_logs.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE)),
                  event=event, club=club, clubs=clubs, club_names=names)
