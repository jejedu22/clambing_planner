"""Adhésions HelloAsso : synchronisation, validité, création des comptes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ClubIntegration, Membership, utcnow
from . import crypto
from .helloasso import HelloAsso, HelloAssoError


def client_for(integ: ClubIntegration | None) -> HelloAsso:
    if integ is None or not integ.helloasso_ready:
        raise HelloAssoError("HelloAsso n'est pas configuré pour ce club.")
    secret = crypto.decrypt(integ.ha_client_secret_enc)
    if secret is None:
        raise HelloAssoError("Secret HelloAsso illisible : saisissez-le de nouveau.")
    return HelloAsso(integ.ha_client_id, secret)


def sync(db: Session, club_id: int) -> int:
    """Importe les adhésions du formulaire configuré. Retourne le nombre d'adhésions vues."""
    integ = db.get(ClubIntegration, club_id)
    if integ is None or not integ.ha_membership_form:
        raise HelloAssoError("Choisissez d'abord le formulaire d'adhésion.")
    items = client_for(integ).items(integ.ha_org_slug, "Membership", integ.ha_membership_form)
    existing = {m.ha_item_id: m for m in db.scalars(select(Membership).where(Membership.club_id == club_id))}
    count = 0
    for it in items:
        if it.type != "Membership" or not it.email:
            continue   # dons, options... et adhésions sans email exploitable
        count += 1
        m = existing.get(it.id)
        if m is None:
            m = Membership(club_id=club_id, ha_item_id=it.id, email=it.email)
            db.add(m)
        m.email, m.first_name, m.last_name = it.email, it.first_name[:120], it.last_name[:120]
        m.state, m.amount_cents, m.paid_at = it.state[:20], it.amount_cents, it.paid_at
    integ.ha_synced_at = utcnow()
    db.commit()
    return count


def season_start(today: date) -> date:
    """Début de la saison sportive en cours (1er septembre)."""
    return date(today.year if today.month >= 9 else today.year - 1, 9, 1)


def is_current(m: Membership, mode: str, today: date) -> bool:
    if m.state not in ("Processed", "Registered") or m.paid_at is None:
        return False
    paid = m.paid_at.astimezone(timezone.utc).date()
    limit = season_start(today) if mode == "season" else today - timedelta(days=365)
    return paid >= limit


def status_by_email(db: Session, club_id: int, today: date) -> dict[str, tuple[Membership, bool]]:
    """Pour chaque email : (adhésion la plus pertinente, à jour ?). Une adhésion valide prime."""
    integ = db.get(ClubIntegration, club_id)
    mode = integ.membership_mode if integ else "season"
    best: dict[str, tuple[Membership, bool]] = {}
    for m in db.scalars(select(Membership).where(Membership.club_id == club_id)):
        cur = is_current(m, mode, today)
        prev = best.get(m.email)
        later = prev is None or (m.paid_at or datetime.min.replace(tzinfo=timezone.utc)) > \
            (prev[0].paid_at or datetime.min.replace(tzinfo=timezone.utc))
        if prev is None or (cur and not prev[1]) or (cur == prev[1] and later):
            best[m.email] = (m, cur)
    return best
