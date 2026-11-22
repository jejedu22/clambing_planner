"""Journalisation : format texte ou JSON, fichier tournant optionnel, identifiant par requête."""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "request_id"}
_RESET_RE = re.compile(r"^(/reset/).+")


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
                "request_id": getattr(record, "request_id", "-")}
        data.update({k: v for k, v in record.__dict__.items() if k not in _STD})   # champs structurés (extra=)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = " ".join(f"{k}={v}" for k, v in record.__dict__.items() if k not in _STD)
        return f"{base} {extra}".rstrip()


def setup_logging() -> None:
    """Configure le logger racine (idempotent)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = JsonFormatter() if settings.log_format == "json" else TextFormatter(
        "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    problem = None
    if settings.log_file:
        try:
            path = Path(settings.log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                path, maxBytes=settings.log_max_mb * 1024 * 1024, backupCount=settings.log_backups, encoding="utf-8"))
        except OSError as exc:
            problem = f"Fichier de log inutilisable ({exc}) : sortie standard uniquement"
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(ContextFilter())
        root.addHandler(h)
    level = getattr(logging, settings.log_level, logging.INFO)
    root.setLevel(level if isinstance(level, int) else logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):   # un seul circuit de logs, notre format
        lg = logging.getLogger(name)
        lg.handlers, lg.propagate = [], True
    # Le journal d'accès d'uvicorn contient le chemin complet (donc les jetons /reset/<jeton>) :
    # il est coupé, RequestLogMiddleware journalise les requêtes avec les secrets masqués.
    access = logging.getLogger("uvicorn.access")
    access.handlers, access.propagate, access.disabled = [], False, True
    logging.getLogger("httpx").setLevel(logging.WARNING)   # httpx journalise les URL complètes
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if problem:
        logging.getLogger("app").warning(problem)


def safe_path(path: str) -> str:
    """Chemin sans secret : le jeton des liens /reset/<jeton> n'est jamais journalisé."""
    return _RESET_RE.sub(r"\1[masqué]", path)


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "?"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


class RequestLogMiddleware:
    """Middleware ASGI : identifiant de requête, durée, statut, utilisateur. Jamais de query string."""

    def __init__(self, app) -> None:
        self.app = app
        self.log = logging.getLogger("app.request")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex[:12]    # généré ici : un X-Request-ID entrant n'est pas de confiance
        request_id_var.set(rid)
        start, status = time.perf_counter(), 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        path = safe_path(scope.get("path", ""))
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            self.log.exception("Erreur non gérée", extra={"method": scope["method"], "path": path})
            raise
        finally:
            quiet = path == "/healthz" or path.startswith("/static/")
            level = (logging.DEBUG if quiet else logging.ERROR if status >= 500 else
                     logging.INFO if status < 400 or status == 404 else logging.WARNING)
            if self.log.isEnabledFor(level):
                # request.state est partagé via le scope ; un ContextVar ne remonte pas des threads sync
                u = (scope.get("state") or {}).get("user")
                client = scope.get("client")
                self.log.log(level, "%s %s -> %s", scope["method"], path, status, extra={
                    "status": status, "ms": round((time.perf_counter() - start) * 1000, 1),
                    "user": getattr(u, "id", None), "ip": client[0] if client else None})
