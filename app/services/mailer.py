"""Envoi d'emails via Mailjet (API v3.1), avec les clés propres à chaque club."""
from __future__ import annotations

import html as html_lib
import logging
import re
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import ClubIntegration
from . import crypto

log = logging.getLogger("mailer")
BATCH = 50  # limite de messages par appel Mailjet

EMAIL_RE = re.compile(r"^[^@\s<>\",;]+@[^@\s<>\",;]+\.[^@\s<>\",;]+$")


@dataclass
class Mail:
    to_email: str
    to_name: str
    subject: str
    text: str
    html: str


@dataclass
class MailConfig:
    api_key: str
    api_secret: str
    from_email: str
    from_name: str


def load_config(db: Session, club_id: int | None) -> MailConfig | None:
    """Configuration Mailjet du club, ou None si elle est absente/illisible."""
    if club_id is None:
        return None
    integ = db.get(ClubIntegration, club_id)
    if integ is None or not integ.mailjet_ready:
        return None
    secret = crypto.decrypt(integ.mj_api_secret_enc)
    if secret is None:
        log.error("Secret Mailjet illisible pour le club %s (SECRET_KEY modifiée ?)", club_id)
        return None
    return MailConfig(integ.mj_api_key, secret, integ.mj_from_email, integ.mj_from_name or "Climbing Planner")


def _post(url: str, payload: dict, auth: tuple[str, str]) -> tuple[int, dict]:
    """Appel HTTP isolé pour pouvoir être remplacé dans les tests."""
    r = httpx.post(url, json=payload, auth=auth, timeout=20, headers={"User-Agent": settings.user_agent})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def _line(text: str) -> str:
    return " ".join(text.split())[:250]  # pas de retour à la ligne dans les champs courts


def send(cfg: MailConfig, mails: list[Mail], sandbox: bool = False) -> tuple[int, str | None]:
    """Envoie les emails. Retourne (nombre accepté, message d'erreur éventuel). Ne lève jamais."""
    sent, error = 0, None
    for i in range(0, len(mails), BATCH):
        chunk = mails[i:i + BATCH]
        payload: dict = {"Messages": [{
            "From": {"Email": cfg.from_email, "Name": _line(cfg.from_name)},
            "To": [{"Email": m.to_email, "Name": _line(m.to_name)}],
            "Subject": _line(m.subject), "TextPart": m.text, "HTMLPart": m.html,
        } for m in chunk]}
        if sandbox:
            payload["SandboxMode"] = True
        try:
            status, data = _post(f"{settings.mailjet_url}/v3.1/send", payload, (cfg.api_key, cfg.api_secret))
        except httpx.HTTPError as exc:
            error = f"Mailjet injoignable ({type(exc).__name__})"
            continue
        results = data.get("Messages") or []
        sent += sum(1 for m in results if m.get("Status") == "success")
        if status == 401:
            error = "Clés API Mailjet refusées (401)."
        elif status >= 400 or any(m.get("Status") != "success" for m in results):
            err = next((e.get("ErrorMessage") for m in results for e in m.get("Errors", [])), None)
            error = err or data.get("ErrorMessage") or f"Mailjet a répondu {status}."
    if error:
        log.warning("Envoi Mailjet partiel ou en échec : %s", error)
    return sent, error


def send_for_club(club_id: int | None, mails: list[Mail]) -> int:
    """Envoi en tâche de fond (ouvre sa propre session). Sans config Mailjet : ne fait rien."""
    if not mails:
        return 0
    with SessionLocal() as db:
        cfg = load_config(db, club_id)
    if cfg is None:
        return 0
    return send(cfg, mails)[0]


def build(subject: str, name: str, paragraphs: list[str], cta: tuple[str, str] | None = None,
          footer: str | None = None) -> tuple[str, str]:
    """Construit les versions texte et HTML (tout le contenu dynamique est échappé)."""
    esc = html_lib.escape
    text = [f"Bonjour {name},", ""] + [p for p in paragraphs for p in (p, "")]
    if cta:
        text += [f"{cta[0]} : {cta[1]}", ""]
    if footer:
        text.append(footer)
    body = "".join(f'<p style="margin:0 0 12px">{esc(p)}</p>' for p in paragraphs)
    button = (f'<p style="margin:20px 0"><a href="{esc(cta[1], quote=True)}" style="background:#c2540a;color:#fff;'
              f'padding:10px 18px;border-radius:6px;text-decoration:none">{esc(cta[0])}</a></p>'
              f'<p style="font-size:12px;color:#666">Lien : {esc(cta[1])}</p>') if cta else ""
    foot = f'<p style="font-size:12px;color:#666;margin-top:24px">{esc(footer)}</p>' if footer else ""
    page = (f'<div style="font:15px/1.5 system-ui,sans-serif;color:#222;max-width:560px">'
            f'<h2 style="color:#c2540a;margin:0 0 16px">{esc(subject)}</h2><p>Bonjour {esc(name)},</p>'
            f'{body}{button}{foot}</div>')
    return "\n".join(text).strip(), page


def link(path: str) -> str | None:
    """Lien absolu vers l'application (None si APP_BASE_URL n'est pas configurée)."""
    return f"{settings.app_base_url}{path}" if settings.app_base_url else None
