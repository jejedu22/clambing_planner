"""Emails de l'application : notifications de sorties, rappels, réinitialisation, invitations."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..db import SessionLocal
from ..models import Outing, OutingReminder, Participation, User, UserPref, utcnow
from . import mailer, tokens, weather
from ..web import fr_date, fr_time

log = logging.getLogger("notifications")
FOOTER = ("Vous recevez cet email parce que vous êtes membre d'un club sur Climbing Planner. "
          "Vous pouvez désactiver ces notifications dans « Mon compte ».")


def _opted_out(db: Session) -> set[int]:
    return set(db.scalars(select(UserPref.user_id).where(UserPref.notify_email.is_(False))))


def _club_members(db: Session, club_id: int, exclude: int | None) -> list[User]:
    out = _opted_out(db)
    users = db.scalars(select(User).where(User.club_id == club_id, User.is_active.is_(True))).all()
    return [u for u in users if u.id != exclude and u.id not in out]


def _outing(db: Session, outing_id: int) -> Outing | None:
    return db.scalar(select(Outing).options(joinedload(Outing.site), joinedload(Outing.club))
                     .where(Outing.id == outing_id))


def _details(o: Outing) -> list[str]:
    when = fr_date(o.day) + (f" à {fr_time(o.meeting_time)}" if o.meeting_time else "")
    lines = [f"Quand : {when}", f"Où : {o.site.display_name}"]
    if o.meeting_point:
        lines.append(f"Rendez-vous : {o.meeting_point}")
    return lines


def _mails(users: list[User], o: Outing, subject: str, intro: str, extra: list[str] | None = None) -> list[mailer.Mail]:
    url = mailer.link(f"/outings/{o.id}")
    out = []
    for u in users:
        text, html = mailer.build(subject, u.name, [intro, *_details(o), *(extra or [])],
                                  ("Voir la sortie", url) if url else None, FOOTER)
        out.append(mailer.Mail(u.email, u.name, subject, text, html))
    return out


def outing_created(outing_id: int, actor_id: int | None) -> None:
    with SessionLocal() as db:
        o = _outing(db, outing_id)
        if o is None or o.cancelled:
            return
        mails = _mails(_club_members(db, o.club_id, actor_id), o, f"Nouvelle sortie : {o.title}",
                       f"Une nouvelle sortie est proposée : {o.title}.",
                       [f"Places : {o.max_participants}"] if o.max_participants else None)
        club_id = o.club_id
    mailer.send_for_club(club_id, mails)


def _participants(db: Session, o: Outing, actor_id: int | None) -> list[User]:
    out = _opted_out(db)
    rows = db.scalars(select(Participation).options(joinedload(Participation.user))
                      .where(Participation.outing_id == o.id, Participation.status.in_(("yes", "maybe")))).all()
    return [p.user for p in rows if p.user.is_active and p.user_id != actor_id and p.user_id not in out]


def outing_changed(outing_id: int, actor_id: int | None, changes: list[str]) -> None:
    with SessionLocal() as db:
        o = _outing(db, outing_id)
        if o is None or o.cancelled:
            return
        mails = _mails(_participants(db, o, actor_id), o, f"Sortie modifiée : {o.title}",
                       f"La sortie « {o.title} » a été modifiée.", ["Changements : " + ", ".join(changes)])
        club_id = o.club_id
    mailer.send_for_club(club_id, mails)


def outing_cancelled(outing_id: int, actor_id: int | None, cancelled: bool) -> None:
    with SessionLocal() as db:
        o = _outing(db, outing_id)
        if o is None:
            return
        if cancelled:
            subject, intro = f"Sortie annulée : {o.title}", f"La sortie « {o.title} » est annulée."
        else:
            subject, intro = f"Sortie rétablie : {o.title}", f"La sortie « {o.title} » est de nouveau d'actualité."
        mails = _mails(_participants(db, o, actor_id), o, subject, intro)
        club_id = o.club_id
    mailer.send_for_club(club_id, mails)


def send_reminders() -> int:
    """Rappel la veille de la sortie aux participants (oui / peut-être), avec la météo. Retourne le nb d'emails."""
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    if not 8 <= now.hour < 20:   # pas d'email en pleine nuit
        return 0
    tomorrow = (now + timedelta(days=1)).date()
    total = 0
    with SessionLocal() as db:
        done = set(db.scalars(select(OutingReminder.outing_id)))
        outings = db.scalars(select(Outing).options(joinedload(Outing.site), joinedload(Outing.club))
                             .where(Outing.day == tomorrow, Outing.cancelled.is_(False))).all()
        for o in outings:
            if o.id in done:
                continue
            db.add(OutingReminder(outing_id=o.id))   # marqué d'abord : jamais de doublon
            db.commit()
            f = weather.forecast_for_day(o.site.lat, o.site.lon, o.day)
            extra = [f"Météo prévue : {f['label']}, {round(f['tmin'])}° / {round(f['tmax'])}° — {f['verdict_text']}"] if f else None
            mails = _mails(_participants(db, o, None), o, f"Rappel demain : {o.title}",
                           f"Rappel : la sortie « {o.title} » a lieu demain.", extra)
            total += mailer.send_for_club(o.club_id, mails)
    return total


# --- Transactionnel : jamais soumis aux préférences de notification ----------

def send_reset(user_id: int) -> bool:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            return False
        cfg = mailer.load_config(db, user.club_id)
        base = mailer.link("/reset/")
        if cfg is None or base is None:
            log.warning("Réinitialisation demandée mais Mailjet/APP_BASE_URL non configuré (utilisateur %s)", user.id)
            return False
        raw = tokens.create_token(db, user, "reset")
        subject = "Réinitialisation de votre mot de passe"
        text, html = mailer.build(subject, user.name, [
            "Une réinitialisation de votre mot de passe a été demandée. Le lien est valable 1 heure.",
            "Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet email."],
            ("Choisir un nouveau mot de passe", base + raw))
        mail = mailer.Mail(user.email, user.name, subject, text, html)
    return mailer.send(cfg, [mail])[0] == 1


def send_invite(db: Session, user: User) -> tuple[bool, str | None]:
    """Invitation à définir son mot de passe (synchrone : l'administrateur voit le résultat)."""
    cfg = mailer.load_config(db, user.club_id)
    base = mailer.link("/reset/")
    if cfg is None:
        return False, "Mailjet n'est pas configuré pour ce club."
    if base is None:
        return False, "APP_BASE_URL n'est pas configurée sur le serveur."
    raw = tokens.create_token(db, user, "invite")
    club = user.club.name if user.club else "votre club"
    subject = f"Invitation à rejoindre {club} sur Climbing Planner"
    text, html = mailer.build(subject, user.name, [
        f"Un compte Climbing Planner a été créé pour vous au sein de {club}.",
        "Choisissez votre mot de passe grâce au lien ci-dessous (valable 7 jours)."],
        ("Activer mon compte", base + raw))
    sent, err = mailer.send(cfg, [mailer.Mail(user.email, user.name, subject, text, html)])
    return sent == 1, err


def reminder_loop() -> None:
    import time
    time.sleep(30)
    while True:
        try:
            send_reminders()
        except Exception:  # noqa: BLE001
            log.exception("Boucle des rappels")
        time.sleep(1800)
