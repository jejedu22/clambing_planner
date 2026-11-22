"""Journal d'audit : écrit dans les logs (logger « audit ») et en base. Ne lève jamais d'exception."""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import Request
from sqlalchemy import delete

from .config import settings
from .db import SessionLocal
from .logging_setup import mask_email
from .models import AuditLog, User, utcnow

log = logging.getLogger("audit")


def audit(event: str, request: Request | None = None, *, actor: User | None = None,
          club_id: int | None = None, target: str | None = None, detail: str | None = None) -> None:
    """Enregistre un événement. `detail` ne doit jamais contenir de secret ni de mot de passe."""
    actor = actor or (getattr(request.state, "user", None) if request is not None else None)
    ip = request.client.host if request is not None and request.client else None
    if club_id is None and actor is not None:
        club_id = actor.club_id
    label = f"{actor.name} <{mask_email(actor.email)}>" if actor else None
    log.info(event, extra={"event": event, "actor": actor.id if actor else None, "club": club_id,
                           "target": target, "detail": detail, "ip": ip})
    try:
        with SessionLocal() as db:
            db.add(AuditLog(event=event, actor_id=actor.id if actor else None, actor_label=label,
                            club_id=club_id, target=(target or None) and target[:200],
                            detail=(detail or None) and detail[:500], ip=ip))
            db.commit()
    except Exception:  # noqa: BLE001 - l'audit ne doit jamais casser une action
        logging.getLogger("app").exception("Écriture du journal d'audit impossible (%s)", event)


def purge(days: int | None = None) -> int:
    """Supprime les événements plus anciens que la durée de rétention. Retourne le nombre supprimé."""
    days = settings.audit_retention_days if days is None else days
    if days <= 0:
        return 0
    with SessionLocal() as db:
        res = db.execute(delete(AuditLog).where(AuditLog.at < utcnow() - timedelta(days=days)))
        db.commit()
        return res.rowcount or 0
