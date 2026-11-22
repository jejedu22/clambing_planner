"""Envoi d'emails classique (SMTP). Utilisé pour le mot de passe oublié, indépendamment de Mailjet.

Configuration globale du serveur : SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
SMTP_FROM_NAME, SMTP_SECURITY (starttls | ssl | none).
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from ..config import settings
from .mailer import EMAIL_RE, Mail

log = logging.getLogger("smtp")
TIMEOUT = 15


def configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def _deliver(msg: EmailMessage) -> None:
    """Connexion SMTP isolée pour pouvoir être remplacée dans les tests."""
    security = settings.smtp_security
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT,
                                                context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT)
    with server:
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def send(mail: Mail) -> tuple[bool, str | None]:
    """Envoie un email. Retourne (succès, message d'erreur lisible). Ne lève jamais."""
    if not configured():
        return False, "SMTP non configuré (SMTP_HOST / SMTP_FROM)."
    if not EMAIL_RE.match(mail.to_email):
        return False, "Adresse destinataire invalide."
    try:
        msg = EmailMessage()   # refuse les retours à la ligne dans les en-têtes (anti-injection)
        msg["From"] = formataddr((" ".join(settings.smtp_from_name.split()), settings.smtp_from))
        msg["To"] = formataddr((" ".join(mail.to_name.split()), mail.to_email))
        msg["Subject"] = " ".join(mail.subject.split())
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=settings.smtp_from.rpartition("@")[2] or None)
        msg.set_content(mail.text)
        msg.add_alternative(mail.html, subtype="html")
        _deliver(msg)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        log.warning("Envoi SMTP en échec : %s", type(exc).__name__)
        return False, f"Envoi SMTP impossible ({type(exc).__name__})."
    return True, None
