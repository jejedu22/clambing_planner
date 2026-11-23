"""Agenda (accueil) et page « Club »."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..db import get_db
from ..models import AuthToken, ClubIntegration, Outing, Participation, User, utcnow
from ..security import RedirectTo, current_user_optional, login_required
from ..services import memberships, weather
from ..web import render

router = APIRouter()
_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"]
_DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def today() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def week_title(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"Semaine du {monday.day} au {sunday.day} {_MONTHS[sunday.month - 1]}"
    return f"Semaine du {monday.day} {_MONTHS[monday.month - 1]} au {sunday.day} {_MONTHS[sunday.month - 1]}"


def club_todo(db: Session, user: User) -> dict | None:
    """Compteurs « à faire » pour l'administrateur d'un club (None sinon)."""
    if not (user.is_admin and user.club_id):
        return None
    pending_invites = db.scalar(
        select(func.count()).select_from(AuthToken).join(User, User.id == AuthToken.user_id)
        .where(User.club_id == user.club_id, AuthToken.purpose == "invite",
               AuthToken.used_at.is_(None), AuthToken.expires_at > utcnow()))
    expired = None
    integ = db.get(ClubIntegration, user.club_id)
    if integ and integ.ha_membership_form:
        expired = sum(1 for _, ok in memberships.status_by_email(db, user.club_id, today()).values() if not ok)
    return {"invites": pending_invites, "expired": expired}


@router.get("/")
def home(request: Request, week: str = "", user: User | None = Depends(current_user_optional),
         db: Session = Depends(get_db)):
    if user is None:                       # visiteur : page de présentation publique
        from .public import landing
        return landing(request, db)
    if user.must_change_password:
        raise RedirectTo("/password")
    t = today()
    try:
        ref = date.fromisoformat(week) if week else t
    except ValueError:
        ref = t
    monday = ref - timedelta(days=ref.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]

    def scoped(q):
        return q if user.is_super else q.where(Outing.club_id == user.club_id)

    week_outings = db.scalars(scoped(
        select(Outing).options(joinedload(Outing.site), joinedload(Outing.club))
        .where(Outing.day >= days[0], Outing.day <= days[-1])
        .order_by(Outing.day, Outing.meeting_time))).unique().all()
    upcoming = db.scalars(scoped(
        select(Outing).options(joinedload(Outing.site), joinedload(Outing.club), joinedload(Outing.participations))
        .where(Outing.day >= t, Outing.cancelled.is_(False))
        .order_by(Outing.day, Outing.meeting_time).limit(10))).unique().all()

    every = {o.id: o for o in [*week_outings, *upcoming]}
    forecasts = {oid: weather.forecast_for_day(o.site.lat, o.site.lon, o.day)
                 for oid, o in every.items() if t - timedelta(days=3) <= o.day <= t + timedelta(days=15)}
    mine = {p.outing_id: p for p in db.scalars(select(Participation).where(
        Participation.user_id == user.id, Participation.outing_id.in_(list(every) or [0])))}
    stats = {o.id: {"yes": sum(1 for p in o.participations if p.status == "yes") + len(o.guests),
                    "guests": len(o.guests),
                    "seats": sum(p.seats for p in o.participations if p.status == "yes"),
                    "drivers": sum(1 for p in o.participations if p.status == "yes" and p.seats)} for o in upcoming}
    can_reply = {o.id: o.club_id == user.club_id for o in upcoming}
    day_cells = [{"date": d, "label": _DAYS[d.weekday()], "num": d.day, "today": d == t, "past": d < t,
                  "outings": [o for o in week_outings if o.day == d]} for d in days]
    pending = sum(1 for o in upcoming if can_reply[o.id] and o.id not in mine)
    return render(request, "dashboard.html", days=day_cells, title=week_title(monday),
                  prev_week=(monday - timedelta(days=7)).isoformat(), next_week=(monday + timedelta(days=7)).isoformat(),
                  this_week=monday == t - timedelta(days=t.weekday()), week_param=monday.isoformat(),
                  upcoming=upcoming, forecasts=forecasts, mine=mine, stats=stats, can_reply=can_reply,
                  pending=pending, next_outing=upcoming[0] if upcoming else None, todo=club_todo(db, user))


@router.get("/club")
def club_hub(request: Request, user: User = Depends(login_required), db: Session = Depends(get_db)):
    from .requests_admin import pending_count
    return render(request, "club.html", todo=club_todo(db, user), pending_requests=pending_count(db) if user.is_super else 0)
