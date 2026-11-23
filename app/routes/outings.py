from datetime import date, time

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import Club, ClubIntegration, Outing, OutingForm, OutingReminder, Participation, Site, User
from ..security import admin_required, can_access_club, login_required
from ..services import memberships, notifications, weather
from ..services.helloasso import FORM_TYPES, SLUG_RE, HelloAssoError, Item
from ..web import flash, render
from .dashboard import today

router = APIRouter()
STATUSES = {"yes": "Je participe", "maybe": "Peut-être", "no": "Je ne viens pas"}


def get_outing(db: Session, user: User, outing_id: int) -> Outing:
    outing = db.scalar(
        select(Outing).options(joinedload(Outing.site), joinedload(Outing.club))
        .where(Outing.id == outing_id)
    )
    # 404 (et non 403) pour ne pas révéler l'existence des sorties d'autres clubs
    if outing is None or not can_access_club(user, outing.club_id):
        raise HTTPException(404)
    return outing


def _parse(db: Session, user: User, club_id: str, site_id: str, title: str, day: str,
           meeting_time: str, meeting_point: str, description: str, max_participants: str):
    """Valide le formulaire. Retourne (valeurs, erreur)."""
    try:
        cid = int(club_id) if user.is_super else user.club_id
        club = db.get(Club, cid) if cid else None
        site = db.get(Site, int(site_id)) if site_id else None
        d = date.fromisoformat(day)
        t = time.fromisoformat(meeting_time) if meeting_time else None
        maxp = int(max_participants) if max_participants else None
    except (ValueError, TypeError):
        return None, "Certains champs sont invalides."
    if club is None:
        return None, "Choisissez un club."
    if site is None:
        return None, "Choisissez un site d'escalade."
    if not title.strip():
        return None, "Le titre est obligatoire."
    if maxp is not None and not 1 <= maxp <= 500:
        return None, "Nombre de participants invalide."
    return dict(club_id=club.id, site_id=site.id, title=title.strip()[:160], day=d, meeting_time=t,
                meeting_point=meeting_point.strip()[:255] or None,
                description=description.strip()[:5000] or None, max_participants=maxp), None


@router.get("/outings")
def outings_list(request: Request, past: int = 0, club: int | None = None,
                 user: User = Depends(login_required), db: Session = Depends(get_db)):
    q = select(Outing).options(joinedload(Outing.site), joinedload(Outing.club),
                               joinedload(Outing.participations))
    q = q.where(Outing.day < today()) if past else q.where(Outing.day >= today())
    q = q.order_by(Outing.day.desc() if past else Outing.day, Outing.meeting_time)
    if user.is_super:
        if club:
            q = q.where(Outing.club_id == club)
    else:
        q = q.where(Outing.club_id == user.club_id)
    outings = db.scalars(q.limit(200)).unique().all()
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    return render(request, "outings.html", outings=outings, past=past, clubs=clubs, club=club)


@router.get("/outings/new")
def outing_new(request: Request, site: int | None = None,
               user: User = Depends(admin_required), db: Session = Depends(get_db)):
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    return render(request, "outing_form.html", outing=None, clubs=clubs,
                  site=db.get(Site, site) if site else None)


@router.post("/outings/new")
def outing_create(request: Request, background: BackgroundTasks, club_id: str = Form(""), site_id: str = Form(""),
                  title: str = Form(""), day: str = Form(""), meeting_time: str = Form(""),
                  meeting_point: str = Form(""), description: str = Form(""),
                  max_participants: str = Form(""),
                  user: User = Depends(admin_required), db: Session = Depends(get_db)):
    values, error = _parse(db, user, club_id, site_id, title, day, meeting_time,
                           meeting_point, description, max_participants)
    if error:
        clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
        site = db.get(Site, int(site_id)) if site_id.isdigit() else None
        return render(request, "outing_form.html", 400, outing=None, clubs=clubs, site=site,
                      error=error, form=dict(club_id=club_id, title=title, day=day,
                      meeting_time=meeting_time, meeting_point=meeting_point,
                      description=description, max_participants=max_participants))
    outing = Outing(**values, created_by=user.id)
    db.add(outing)
    db.commit()
    background.add_task(notifications.outing_created, outing.id, user.id)
    flash(request, "Sortie créée.", "ok")
    return RedirectResponse(f"/outings/{outing.id}", status_code=303)


@router.get("/outings/{outing_id}")
def outing_detail(outing_id: int, request: Request, user: User = Depends(login_required),
                  db: Session = Depends(get_db)):
    outing = get_outing(db, user, outing_id)
    parts = db.scalars(
        select(Participation).options(joinedload(Participation.user))
        .where(Participation.outing_id == outing.id).order_by(Participation.updated_at)
    ).all()
    groups = {s: [p for p in parts if p.status == s] for s in STATUSES}
    mine = next((p for p in parts if p.user_id == user.id), None)
    can_edit = user.is_super or (user.is_admin and user.club_id == outing.club_id)
    from .guests import active_link, share_url
    from ..services import crypto
    link = active_link(db, outing.id) if can_edit else None
    raw = crypto.decrypt(link.token_enc) if link else None
    return render(request, "outing_detail.html", outing=outing, groups=groups, mine=mine,
                  guests=outing.guests, share=share_url(request, raw) if raw else None,
                  share_open=not outing.cancelled and outing.day >= today(),
                  statuses=STATUSES, seats=sum(p.seats for p in groups["yes"]),
                  forecast=weather.forecast_for_day(outing.site.lat, outing.site.lon, outing.day),
                  can_edit=can_edit,
                  ha_ready=bool((i := db.get(ClubIntegration, outing.club_id)) and i.helloasso_ready))


@router.post("/outings/{outing_id}/respond")
def outing_respond(outing_id: int, request: Request, status: str = Form(...),
                   seats: int = Form(0), next: str = Form(""), user: User = Depends(login_required),
                   db: Session = Depends(get_db)):
    from .favorites import safe_next
    outing = get_outing(db, user, outing_id)
    back = safe_next(next, f"/outings/{outing.id}")
    if status not in STATUSES:
        raise HTTPException(400)
    if outing.cancelled or outing.day < today():
        flash(request, "Cette sortie n'accepte plus de réponses.", "error")
        return RedirectResponse(back, status_code=303)
    part = db.scalar(select(Participation).where(
        Participation.outing_id == outing.id, Participation.user_id == user.id))
    if status == "yes" and outing.max_participants and not (part and part.status == "yes"):
        from .guests import taken
        if taken(db, outing) >= outing.max_participants:
            flash(request, "La sortie est complète.", "error")
            return RedirectResponse(back, status_code=303)
    if part is None:
        part = Participation(outing_id=outing.id, user_id=user.id, status=status)
        db.add(part)
    part.status = status
    part.seats = max(0, min(seats, 8)) if status == "yes" else 0
    db.commit()
    flash(request, "Réponse enregistrée.", "ok")
    return RedirectResponse(back, status_code=303)


def _editable(db: Session, user: User, outing_id: int) -> Outing:
    outing = get_outing(db, user, outing_id)
    if not user.is_admin:
        raise HTTPException(403, "Réservé aux administrateurs.")
    return outing


@router.get("/outings/{outing_id}/edit")
def outing_edit(outing_id: int, request: Request, user: User = Depends(login_required),
                db: Session = Depends(get_db)):
    outing = _editable(db, user, outing_id)
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    return render(request, "outing_form.html", outing=outing, clubs=clubs, site=outing.site)


@router.post("/outings/{outing_id}/edit")
def outing_update(outing_id: int, request: Request, background: BackgroundTasks, club_id: str = Form(""),
                  site_id: str = Form(""), title: str = Form(""), day: str = Form(""),
                  meeting_time: str = Form(""), meeting_point: str = Form(""),
                  description: str = Form(""), max_participants: str = Form(""),
                  user: User = Depends(login_required), db: Session = Depends(get_db)):
    outing = _editable(db, user, outing_id)
    values, error = _parse(db, user, club_id or str(outing.club_id), site_id, title, day,
                           meeting_time, meeting_point, description, max_participants)
    if error:
        clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
        return render(request, "outing_form.html", 400, outing=outing, clubs=clubs,
                      site=outing.site, error=error)
    before = (outing.day, outing.meeting_time, outing.site_id, outing.meeting_point)
    for key, value in values.items():
        setattr(outing, key, value)
    changes = [label for label, old, new in (
        ("date", before[0], outing.day), ("heure", before[1], outing.meeting_time),
        ("site", before[2], outing.site_id), ("lieu de rendez-vous", before[3], outing.meeting_point)) if old != new]
    db.commit()
    if changes:
        background.add_task(notifications.outing_changed, outing.id, user.id, changes)
    flash(request, "Sortie modifiée.", "ok")
    return RedirectResponse(f"/outings/{outing.id}", status_code=303)


@router.post("/outings/{outing_id}/cancel")
def outing_cancel(outing_id: int, request: Request, background: BackgroundTasks, user: User = Depends(login_required),
                  db: Session = Depends(get_db)):
    outing = _editable(db, user, outing_id)
    outing.cancelled = not outing.cancelled
    db.commit()
    background.add_task(notifications.outing_cancelled, outing.id, user.id, outing.cancelled)
    flash(request, "Sortie annulée." if outing.cancelled else "Sortie rétablie.", "ok")
    return RedirectResponse(f"/outings/{outing.id}", status_code=303)


@router.post("/outings/{outing_id}/delete")
def outing_delete(outing_id: int, request: Request, user: User = Depends(login_required),
                  db: Session = Depends(get_db)):
    outing = _editable(db, user, outing_id)
    for model in (OutingForm, OutingReminder):
        row = db.get(model, outing.id)
        if row:
            db.delete(row)
    db.delete(outing)
    db.commit()
    flash(request, "Sortie supprimée.", "ok")
    return RedirectResponse("/outings", status_code=303)


# --- Paiements HelloAsso d'une sortie ---------------------------------------

_items_cache: dict[tuple, tuple[float, list[Item]]] = {}
CACHE_TTL = 60


def _paying_items(integ: ClubIntegration, link: OutingForm, refresh: bool) -> list[Item]:
    import time
    key = (integ.club_id, link.form_type, link.form_slug)
    hit = _items_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    items = memberships.client_for(integ).items(integ.ha_org_slug, link.form_type, link.form_slug)
    _items_cache[key] = (time.time(), items)
    return items


def _payments_outing(db: Session, user: User, outing_id: int) -> tuple[Outing, ClubIntegration]:
    outing = _editable(db, user, outing_id)
    integ = db.get(ClubIntegration, outing.club_id)
    if integ is None or not integ.helloasso_ready:
        raise HTTPException(404, "HelloAsso n'est pas configuré pour ce club.")
    return outing, integ


@router.get("/outings/{outing_id}/payments")
def outing_payments(outing_id: int, request: Request, refresh: int = 0, user: User = Depends(login_required),
                    db: Session = Depends(get_db)):
    outing, integ = _payments_outing(db, user, outing_id)
    link = db.get(OutingForm, outing.id)
    ctx: dict = dict(outing=outing, link=link, forms=None, rows=[], unmatched=[], error=None)
    try:
        if link is None:
            ctx["forms"] = [f for f in memberships.client_for(integ).forms(integ.ha_org_slug)
                            if f["type"] != "Membership"]
        else:
            paid: dict[str, dict] = {}
            for it in _paying_items(integ, link, bool(refresh)):
                if it.valid and it.email:
                    p = paid.setdefault(it.email, {"cents": 0, "name": f"{it.first_name} {it.last_name}".strip()})
                    p["cents"] += it.amount_cents
            parts = db.scalars(select(Participation).options(joinedload(Participation.user)).where(
                Participation.outing_id == outing.id, Participation.status == "yes")).all()
            people_rows = [p.user for p in parts] + list(outing.guests)
            emails = {u.email for u in people_rows}
            ctx["rows"] = sorted(((u, paid.get(u.email)) for u in people_rows), key=lambda r: r[0].name.lower())
            club_emails = {u.email for u in db.scalars(select(User).where(User.club_id == outing.club_id))}
            ctx["unmatched"] = [(e, v) for e, v in paid.items() if e not in emails and e not in club_emails]
    except HelloAssoError as exc:
        ctx["error"] = str(exc)
    return render(request, "outing_payments.html", **ctx)


@router.post("/outings/{outing_id}/payments/link")
def outing_payments_link(outing_id: int, request: Request, form: str = Form(...),
                         user: User = Depends(login_required), db: Session = Depends(get_db)):
    outing, integ = _payments_outing(db, user, outing_id)
    ftype, _, slug = form.partition("|")
    if ftype not in FORM_TYPES or ftype == "Membership" or not SLUG_RE.match(slug):
        raise HTTPException(400, "Formulaire invalide.")
    try:   # le formulaire doit exister chez HelloAsso pour cette organisation
        match = next((f for f in memberships.client_for(integ).forms(integ.ha_org_slug)
                      if f["type"] == ftype and f["slug"] == slug), None)
    except HelloAssoError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/outings/{outing.id}/payments", status_code=303)
    if match is None:
        raise HTTPException(400, "Formulaire introuvable chez HelloAsso.")
    db.merge(OutingForm(outing_id=outing.id, form_type=ftype, form_slug=slug, form_title=(match["title"] or "")[:255]))
    db.commit()
    flash(request, "Formulaire HelloAsso lié à la sortie.", "ok")
    return RedirectResponse(f"/outings/{outing.id}/payments", status_code=303)


@router.post("/outings/{outing_id}/payments/unlink")
def outing_payments_unlink(outing_id: int, request: Request, user: User = Depends(login_required),
                           db: Session = Depends(get_db)):
    outing, _ = _payments_outing(db, user, outing_id)
    link = db.get(OutingForm, outing.id)
    if link:
        db.delete(link)
        db.commit()
    flash(request, "Formulaire délié.", "ok")
    return RedirectResponse(f"/outings/{outing.id}/payments", status_code=303)
