import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..logging_setup import mask_email
from ..models import User, UserPref
from ..security import (
    DUMMY_HASH, current_user_optional, hash_password, login_required, password_error,
    session_epoch, verify_password,
)
from ..services import tokens
from ..web import flash, render

router = APIRouter()

_failures: dict[str, list[float]] = {}
MAX_FAILURES, WINDOW = 5, 900  # 5 échecs par 15 minutes (par IP + email)


def _recent_failures(key: str) -> int:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < WINDOW]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return len(recent)


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(current_user_optional)):
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    email = email.strip().lower()
    key = f"{request.client.host if request.client else '?'}|{email}"
    if _recent_failures(key) >= MAX_FAILURES:
        audit("login.locked", request, target=mask_email(email))
        return render(request, "login.html", 429, error="Trop de tentatives, réessayez dans quelques minutes.")

    user = db.scalar(select(User).where(User.email == email))
    valid = verify_password(password, user.password_hash if user else DUMMY_HASH)
    if not (user and valid and user.is_active):
        _failures.setdefault(key, []).append(time.time())
        audit("login.failure", request, target=mask_email(email), club_id=user.club_id if user else None)
        return render(request, "login.html", 401, error="Email ou mot de passe incorrect.", email=email)

    _failures.pop(key, None)
    request.session.clear()  # nouvelle session après connexion
    request.session["uid"] = user.id
    request.session["ep"] = session_epoch(db, user.id)
    audit("login.success", request, actor=user, target=f"user:{user.id}")
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    audit("logout", request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/password")
def password_form(request: Request, user: User = Depends(login_required), db: Session = Depends(get_db)):
    pref = db.get(UserPref, user.id)
    return render(request, "password.html", notify=pref.notify_email if pref else True)


@router.post("/password")
def password_change(request: Request, current: str = Form(...), new: str = Form(...),
                    confirm: str = Form(...), user: User = Depends(login_required),
                    db: Session = Depends(get_db)):
    error = None
    if not verify_password(current, user.password_hash):
        error = "Mot de passe actuel incorrect."
    elif new != confirm:
        error = "Les deux mots de passe ne correspondent pas."
    else:
        error = password_error(new)
    if error:
        pref = db.get(UserPref, user.id)
        return render(request, "password.html", 400, error=error, notify=pref.notify_email if pref else True)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    epoch = tokens.bump_epoch(db, user.id)   # déconnecte les autres appareils
    db.commit()
    request.session["ep"] = epoch
    audit("password.changed", request, target=f"user:{user.id}")
    flash(request, "Mot de passe modifié. Les autres appareils ont été déconnectés.", "ok")
    return RedirectResponse("/", status_code=303)
