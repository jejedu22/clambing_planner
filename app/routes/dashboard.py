from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..db import get_db
from ..models import Outing, User
from ..security import login_required
from ..services import weather
from ..web import render

router = APIRouter()


def today():
    return datetime.now(ZoneInfo(settings.timezone)).date()


@router.get("/")
def home(request: Request, user: User = Depends(login_required), db: Session = Depends(get_db)):
    q = (select(Outing).options(joinedload(Outing.site), joinedload(Outing.club),
                                joinedload(Outing.participations))
         .where(Outing.day >= today(), Outing.cancelled.is_(False))
         .order_by(Outing.day, Outing.meeting_time).limit(8))
    if not user.is_super:
        q = q.where(Outing.club_id == user.club_id)
    outings = db.scalars(q).unique().all()
    forecasts = {o.id: weather.forecast_for_day(o.site.lat, o.site.lon, o.day) for o in outings}
    mine = {p.outing_id: p.status for o in outings for p in o.participations if p.user_id == user.id}
    return render(request, "dashboard.html", outings=outings, forecasts=forecasts, mine=mine)
