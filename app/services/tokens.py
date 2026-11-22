"""Jetons à usage unique pour le mot de passe oublié et les invitations."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import AuthToken, User, UserPref, utcnow

RESET_TTL = timedelta(hours=1)
INVITE_TTL = timedelta(days=7)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_token(db: Session, user: User, purpose: str) -> str:
    """Crée un jeton (les précédents non utilisés du même type sont invalidés)."""
    now = utcnow()
    db.execute(update(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == purpose,
                                       AuthToken.used_at.is_(None)).values(used_at=now))
    raw = secrets.token_urlsafe(32)
    db.add(AuthToken(user_id=user.id, token_hash=_hash(raw), purpose=purpose,
                     expires_at=now + (INVITE_TTL if purpose == "invite" else RESET_TTL)))
    db.commit()
    return raw


def lookup(db: Session, raw: str) -> tuple[AuthToken, User] | None:
    """Jeton valide (existant, non utilisé, non expiré, utilisateur actif) ou None."""
    tok = db.scalar(select(AuthToken).where(AuthToken.token_hash == _hash(raw)))
    if tok is None or tok.used_at is not None:
        return None
    exp = tok.expires_at if tok.expires_at.tzinfo else tok.expires_at.replace(tzinfo=timezone.utc)
    if exp < utcnow():
        return None
    user = db.get(User, tok.user_id)
    if user is None or not user.is_active:
        return None
    return tok, user


def bump_epoch(db: Session, user_id: int) -> int:
    """Invalide toutes les sessions ouvertes de l'utilisateur. Retourne la nouvelle époque."""
    pref = db.get(UserPref, user_id)
    if pref is None:
        pref = UserPref(user_id=user_id, notify_email=True, session_epoch=0)
        db.add(pref)
    pref.session_epoch = (pref.session_epoch or 0) + 1
    return pref.session_epoch
