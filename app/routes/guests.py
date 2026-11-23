"""Partage d'une sortie : inscription de personnes sans compte par un lien sécurisé."""
import hashlib
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..audit import audit
from ..db import SessionLocal, get_db
from ..models import Outing, OutingGuest, OutingShareLink, Participation, User, utcnow
from ..security import Throttle, login_required
from ..services import crypto, mailer, notifications, people, weather
from ..web import flash, render
from .dashboard import today

router = APIRouter()
by_ip = Throttle(limit=10, window=3600)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def share_url(request: Request, raw: str) -> str:
    return mailer.link(f"/sortie/{raw}") or f"{str(request.base_url).rstrip('/')}/sortie/{raw}"


def taken(db: Session, outing: Outing) -> int:
    """Places occupées : membres qui viennent + invités."""
    yes = db.scalar(select(func.count()).select_from(Participation).where(
        Participation.outing_id == outing.id, Participation.status == "yes")) or 0
    guests = db.scalar(select(func.count()).select_from(OutingGuest).where(OutingGuest.outing_id == outing.id)) or 0
    return yes + guests


def active_link(db: Session, outing_id: int) -> OutingShareLink | None:
    return db.scalar(select(OutingShareLink).where(OutingShareLink.outing_id == outing_id,
                                                   OutingShareLink.revoked_at.is_(None)))


def _open_outing(db: Session, raw: str) -> Outing | None:
    """Sortie ouverte aux invités pour ce jeton : lien actif, sortie à venir et non annulée."""
    if not raw or len(raw) > 100:
        return None
    link = db.scalar(select(OutingShareLink).where(OutingShareLink.token_hash == _hash(raw),
                                                   OutingShareLink.revoked_at.is_(None)))
    if link is None:
        return None
    o = db.scalar(select(Outing).options(joinedload(Outing.site), joinedload(Outing.club)).where(Outing.id == link.outing_id))
    return o if o and not o.cancelled and o.day >= today() else None


# --- Administration (depuis la fiche d'une sortie) -------------------------------------------

def _editable(db: Session, user: User, outing_id: int) -> Outing:
    from .outings import _editable as editable
    return editable(db, user, outing_id)


@router.post("/outings/{outing_id}/share")
def share_create(outing_id: int, request: Request, user: User = Depends(login_required), db: Session = Depends(get_db)):
    outing = _editable(db, user, outing_id)
    for old in db.scalars(select(OutingShareLink).where(OutingShareLink.outing_id == outing.id,
                                                        OutingShareLink.revoked_at.is_(None))):
        old.revoked_at = utcnow()                # un seul lien actif : l'ancien cesse de fonctionner
    raw = secrets.token_urlsafe(24)
    db.add(OutingShareLink(outing_id=outing.id, token_hash=_hash(raw), token_enc=crypto.encrypt(raw), created_by=user.id))
    db.commit()
    audit("outing_share.created", request, club_id=outing.club_id, target=f"outing:{outing.id}")
    flash(request, "Lien de partage créé : copiez-le ci-dessous.", "ok")
    return RedirectResponse(f"/outings/{outing.id}#partage", status_code=303)


@router.post("/outings/{outing_id}/share/revoke")
def share_revoke(outing_id: int, request: Request, user: User = Depends(login_required), db: Session = Depends(get_db)):
    outing = _editable(db, user, outing_id)
    link = active_link(db, outing.id)
    if link:
        link.revoked_at = utcnow()
        db.commit()
        audit("outing_share.revoked", request, club_id=outing.club_id, target=f"outing:{outing.id}")
    flash(request, "Lien de partage désactivé.", "ok")
    return RedirectResponse(f"/outings/{outing.id}#partage", status_code=303)


@router.post("/outings/{outing_id}/guests/{guest_id}/remove")
def guest_remove(outing_id: int, guest_id: int, request: Request, user: User = Depends(login_required),
                 db: Session = Depends(get_db)):
    outing = _editable(db, user, outing_id)
    guest = db.get(OutingGuest, guest_id)
    if guest is None or guest.outing_id != outing.id:
        raise HTTPException(404)
    db.delete(guest)
    db.commit()
    audit("outing_guest.removed", request, club_id=outing.club_id, target=f"outing:{outing.id}")
    flash(request, "Invité retiré de la sortie.", "ok")
    return RedirectResponse(f"/outings/{outing.id}", status_code=303)


# --- Pages publiques -----------------------------------------------------------------------------

def _public_ctx(db: Session, o: Outing, token: str) -> dict:
    places = None if not o.max_participants else max(0, o.max_participants - taken(db, o))
    return dict(outing=o, token=token, places=places,
                forecast=weather.forecast_for_day(o.site.lat, o.site.lon, o.day))


@router.get("/sortie/{token}")
def shared_outing(token: str, request: Request, db: Session = Depends(get_db)):
    o = _open_outing(db, token)
    if o is None:
        return render(request, "outing_shared.html", 404, invalid=True)
    return render(request, "outing_shared.html", form={}, **_public_ctx(db, o, token))


def _confirm(outing_id: int, guest_id: int) -> None:
    with SessionLocal() as db:
        g = db.get(OutingGuest, guest_id)
        o = db.scalar(select(Outing).options(joinedload(Outing.site), joinedload(Outing.club), joinedload(Outing.guests))
                      .where(Outing.id == outing_id))
        if g is None or o is None:
            return
        mails = [m for m in notifications._guest_mails(o, f"Inscription confirmée : {o.title}",
                                                      f"Vous êtes inscrit(e) à la sortie « {o.title} » de {o.club.name}.")
                 if m.to_email == g.email]
        club_id = o.club_id
    notifications.send_any(club_id, mails)


@router.post("/sortie/{token}")
def shared_register(token: str, request: Request, background: BackgroundTasks, first_name: str = Form(""),
                    last_name: str = Form(""), email: str = Form(""), phone: str = Form(""), consent: str = Form(""),
                    website: str = Form(""), db: Session = Depends(get_db)):
    o = _open_outing(db, token)
    if o is None:
        return render(request, "outing_shared.html", 404, invalid=True)
    form = dict(first_name=first_name, last_name=last_name, email=email, phone=phone)
    ip = request.client.host if request.client else "?"

    def fail(msg: str, code: int = 400):
        return render(request, "outing_shared.html", code, form=form, error=msg, **_public_ctx(db, o, token))

    if website:                                   # champ piège : robot
        return render(request, "outing_shared.html", outing=o, done=True)
    if by_ip.blocked(ip):
        return fail("Trop d'inscriptions depuis cette connexion. Réessayez plus tard.", 429)
    try:
        first, last, mail, tel = people.validate_person(first_name, last_name, email, phone)
        if not (first and last):
            raise people.ValidationError("Indiquez votre prénom et votre nom.")
    except people.ValidationError as exc:
        return fail(str(exc))
    if consent != "1":
        return fail("Merci d'accepter la transmission de vos coordonnées aux organisateurs.")
    if db.scalar(select(OutingGuest.id).where(OutingGuest.outing_id == o.id, OutingGuest.email == mail)):
        return fail("Cette adresse est déjà inscrite à la sortie.")
    member = db.scalar(select(User).where(User.email == mail, User.club_id == o.club_id))
    if member is not None:
        return fail("Vous êtes membre du club : connectez-vous pour répondre à la sortie.")
    if o.max_participants and taken(db, o) >= o.max_participants:
        return fail("La sortie est complète.", 409)
    raw = secrets.token_urlsafe(24)
    guest = OutingGuest(outing_id=o.id, first_name=first, last_name=last, email=mail, phone=tel,
                        cancel_hash=_hash(raw), cancel_enc=crypto.encrypt(raw))
    db.add(guest)
    db.commit()
    by_ip.add(ip)
    audit("outing_guest.registered", request, club_id=o.club_id, target=f"outing:{o.id}")
    background.add_task(_confirm, o.id, guest.id)
    return render(request, "outing_shared.html", outing=o, done=True, email=mail)


@router.get("/invite/annuler/{token}")
def guest_cancel_form(token: str, request: Request, db: Session = Depends(get_db)):
    g = db.scalar(select(OutingGuest).options(joinedload(OutingGuest.outing)).where(OutingGuest.cancel_hash == _hash(token)))
    if g is None:
        return render(request, "guest_cancel.html", 404, invalid=True)
    return render(request, "guest_cancel.html", guest=g, token=token)


@router.post("/invite/annuler/{token}")
def guest_cancel(token: str, request: Request, db: Session = Depends(get_db)):
    g = db.scalar(select(OutingGuest).options(joinedload(OutingGuest.outing)).where(OutingGuest.cancel_hash == _hash(token)))
    if g is None:
        return render(request, "guest_cancel.html", 404, invalid=True)
    outing = g.outing
    db.delete(g)
    db.commit()
    audit("outing_guest.cancelled", request, club_id=outing.club_id, target=f"outing:{outing.id}")
    return render(request, "guest_cancel.html", done=True, outing=outing)
