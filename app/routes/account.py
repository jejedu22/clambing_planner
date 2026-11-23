"""Mot de passe oublié, activation de compte et préférences de notification."""
from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..logging_setup import mask_email
from ..models import User, UserPref
from ..security import Throttle, hash_password, login_required, password_error
from ..services import notifications, tokens
from ..config import settings
from ..web import THEMES, flash, render

router = APIRouter()
forgot_by_email = Throttle(limit=3, window=3600)
forgot_by_ip = Throttle(limit=15, window=3600)
GENERIC = ("Si un compte correspond à cette adresse, un message vient d'être envoyé. "
           "Pensez à vérifier vos courriers indésirables.")


@router.get("/forgot")
def forgot_form(request: Request):
    return render(request, "forgot.html")


@router.post("/forgot")
def forgot(request: Request, background: BackgroundTasks, email: str = Form(...),
           db: Session = Depends(get_db)):
    email = email.strip().lower()[:255]
    ip = request.client.host if request.client else "?"
    # Même réponse dans tous les cas : on ne révèle pas si le compte existe.
    if forgot_by_ip.blocked(ip) or forgot_by_email.blocked(email):
        audit("password.reset_throttled", request, target=mask_email(email))
    else:
        forgot_by_ip.add(ip)
        forgot_by_email.add(email)
        user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
        audit("password.reset_requested", request, club_id=user.club_id if user else None,
              target=f"user:{user.id}" if user else mask_email(email), detail=None if user else "compte inconnu")
        if user is not None:
            background.add_task(notifications.send_reset, user.id)
    return render(request, "forgot.html", sent=True, message=GENERIC)


def _valid(db: Session, token: str):
    return tokens.lookup(db, token)


@router.get("/reset/{token}")
def reset_form(token: str, request: Request, db: Session = Depends(get_db)):
    found = _valid(db, token)
    if found is None:
        return render(request, "reset.html", 400, invalid=True)
    return render(request, "reset.html", token=token, purpose=found[0].purpose)


@router.post("/reset/{token}")
def reset(token: str, request: Request, new: str = Form(...), confirm: str = Form(...),
          db: Session = Depends(get_db)):
    found = _valid(db, token)
    if found is None:
        return render(request, "reset.html", 400, invalid=True)
    tok, user = found
    error = "Les deux mots de passe ne correspondent pas." if new != confirm else password_error(new)
    if error:
        return render(request, "reset.html", 400, token=token, purpose=tok.purpose, error=error)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    tok.used_at = tokens.utcnow()
    tokens.bump_epoch(db, user.id)   # toutes les sessions existantes sont fermées
    db.commit()
    audit("password.reset_done" if tok.purpose == "reset" else "account.activated", request,
          actor=user, target=f"user:{user.id}")
    request.session.clear()
    flash(request, "Mot de passe défini. Vous pouvez vous connecter.", "ok")
    return RedirectResponse("/login", status_code=303)


@router.post("/account/notifications")
def notifications_pref(request: Request, notify: str = Form(""), user: User = Depends(login_required),
                       db: Session = Depends(get_db)):
    pref = db.get(UserPref, user.id)
    if pref is None:
        pref = UserPref(user_id=user.id, session_epoch=0)
        db.add(pref)
    pref.notify_email = notify == "1"
    db.commit()
    audit("account.notifications", request, target=f"user:{user.id}", detail="on" if pref.notify_email else "off")
    flash(request, "Préférences enregistrées.", "ok")
    return RedirectResponse("/password", status_code=303)


@router.post("/theme")
def set_theme(theme: str = Form("auto"), next: str = Form("")):
    """Thème mémorisé sur l'appareil (cookie) ; accessible aussi sans être connecté."""
    from .favorites import safe_next
    response = RedirectResponse(safe_next(next, "/"), status_code=303)
    if theme in THEMES and theme != "auto":
        response.set_cookie("theme", theme, max_age=365 * 24 * 3600, samesite="lax",
                            secure=settings.cookie_secure, httponly=True)
    else:
        response.delete_cookie("theme")
    return response
