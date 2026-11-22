"""Mots de passe, session, CSRF et contrôle d'accès."""
from __future__ import annotations

import secrets

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

MIN_PASSWORD = 10
MAX_PASSWORD_BYTES = 72  # limite de bcrypt


class RedirectTo(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


DUMMY_HASH = hash_password("dummy-password-for-timing")


def password_error(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Le mot de passe doit contenir au moins {MIN_PASSWORD} caractères."
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return f"Le mot de passe est trop long ({MAX_PASSWORD_BYTES} octets maximum)."
    return None


def generate_password() -> str:
    return secrets.token_urlsafe(12)


# --- CSRF -------------------------------------------------------------------

def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    form = await request.form()
    sent = str(form.get("csrf", ""))
    expected = request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(sent, expected):
        raise HTTPException(403, "Jeton CSRF invalide, rechargez la page.")


# --- Utilisateur courant ----------------------------------------------------

def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is not None and not user.is_active:
        user = None
    request.state.user = user
    return user


def login_required(request: Request, user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        request.session.pop("uid", None)
        raise RedirectTo("/login")
    if user.must_change_password and request.url.path not in ("/password", "/logout"):
        raise RedirectTo("/password")
    return user


def admin_required(user: User = Depends(login_required)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Réservé aux administrateurs.")
    return user


def super_required(user: User = Depends(login_required)) -> User:
    if not user.is_super:
        raise HTTPException(403, "Réservé au super administrateur.")
    return user


def can_access_club(user: User, club_id: int) -> bool:
    return user.is_super or user.club_id == club_id
