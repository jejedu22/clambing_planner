from datetime import date, time
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .security import csrf_token
from .services.people import format_phone

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")

_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"]


def fr_date(d: date, short: bool = False) -> str:
    if short:
        return f"{_DAYS[d.weekday()][:3]}. {d.day} {_MONTHS[d.month - 1][:4]}."
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"


def fr_time(t: time | None) -> str:
    return t.strftime("%Hh%M") if t else ""


templates.env.filters["fr_date"] = fr_date
templates.env.filters["fr_time"] = fr_time
templates.env.filters["phone"] = format_phone


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("flash", []).append([category, message])


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx["csrf"] = csrf_token(request)
    ctx["flashes"] = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
