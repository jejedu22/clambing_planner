"""Import automatique des sites d'escalade français depuis OpenStreetMap (Overpass API)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Site, SiteImport, utcnow

log = logging.getLogger("sites_import")

# Sites d'escalade de France métropolitaine. On écarte les voies individuelles
# (climbing=route/route_bottom/...) qui sont très nombreuses dans OSM.
QUERY = """
[out:json][timeout:240];
area["ISO3166-1"="FR"][admin_level=2]->.fr;
nwr["sport"="climbing"]["climbing"!~"^(route|route_bottom|route_top|pitch)$"](area.fr);
out center tags;
"""

_lock = threading.Lock()
_TYPE_TAGS = {
    "climbing:sport": "sport",
    "climbing:boulder": "boulder",
    "climbing:multipitch": "multipitch",
    "climbing:trad": "trad",
    "climbing:toprope": "toprope",
}


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip()) if value else None
    except ValueError:
        return None


def parse_element(el: dict) -> dict | None:
    """Convertit un élément Overpass en dict de colonnes Site (None si inexploitable)."""
    tags = el.get("tags") or {}
    lat = el.get("lat", (el.get("center") or {}).get("lat"))
    lon = el.get("lon", (el.get("center") or {}).get("lon"))
    if lat is None or lon is None or "type" not in el or "id" not in el:
        return None

    types = [code for tag, code in _TYPE_TAGS.items() if tags.get(tag) not in (None, "no")]
    if tags.get("climbing") == "boulder" and "boulder" not in types:
        types.append("boulder")
    indoor = (
        tags.get("leisure") == "sports_centre"
        or tags.get("indoor") == "yes"
        or tags.get("building") not in (None, "no")
    )
    website = tags.get("website") or tags.get("contact:website")
    if website and not website.lower().startswith(("http://", "https://")):
        website = None  # pas de schémas exotiques (javascript:, data:...)

    return {
        "osm_id": f"{el['type']}/{el['id']}",
        "name": (tags.get("name") or tags.get("name:fr") or "").strip()[:255] or None,
        "lat": float(lat),
        "lon": float(lon),
        "outdoor": not indoor,
        "types": ",".join(types),
        "routes": _int(tags.get("climbing:routes")),
        "rock": (tags.get("rock") or tags.get("geological:rock") or None),
        "website": website[:500] if website else None,
        "description": (tags.get("description:fr") or tags.get("description") or None),
    }


def fetch_elements() -> list[dict]:
    last: Exception | None = None
    for url in settings.overpass_urls:
        try:
            r = httpx.post(
                url,
                data={"data": QUERY},
                headers={"User-Agent": settings.user_agent},
                timeout=httpx.Timeout(300, connect=20),
            )
            r.raise_for_status()
            return r.json()["elements"]
        except Exception as exc:  # noqa: BLE001 - on essaie le miroir suivant
            log.warning("Overpass %s a échoué : %s", url, exc)
            last = exc
    raise RuntimeError(f"Aucun serveur Overpass n'a répondu : {last}")


def upsert_sites(db: Session, elements: list[dict]) -> int:
    existing = {s.osm_id: s for s in db.scalars(select(Site))}
    seen: set[str] = set()
    now = utcnow()
    for el in elements:
        data = parse_element(el)
        if data is None or data["osm_id"] in seen:
            continue
        seen.add(data["osm_id"])
        site = existing.get(data["osm_id"])
        if site is None:
            db.add(Site(**data, updated_at=now))
        else:
            for key, value in data.items():
                setattr(site, key, value)
            site.updated_at = now
    db.commit()
    return len(seen)


def run_import() -> bool:
    """Lance un import. Retourne False si un import est déjà en cours."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        with SessionLocal() as db:
            job = SiteImport()
            db.add(job)
            db.commit()
            try:
                job.count = upsert_sites(db, fetch_elements())
                job.status = "ok"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                log.exception("Import des sites en échec")
                job.status = "error"
                job.error = str(exc)[:2000]
            job.finished_at = utcnow()
            db.commit()
    finally:
        _lock.release()
    return True


def is_running() -> bool:
    return _lock.locked()


def import_due(db: Session) -> bool:
    if settings.sites_refresh_days <= 0:
        return False
    last = db.scalar(
        select(SiteImport.finished_at)
        .where(SiteImport.status == "ok")
        .order_by(SiteImport.finished_at.desc())
        .limit(1)
    )
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return utcnow() - last > timedelta(days=settings.sites_refresh_days)


def refresher_loop() -> None:
    """Thread de fond : (ré)importe les sites au démarrage puis tous les N jours."""
    time.sleep(10)
    while True:
        try:
            with SessionLocal() as db:
                due = import_due(db)
            if due:
                run_import()
        except Exception:  # noqa: BLE001
            log.exception("Boucle de rafraîchissement des sites")
        time.sleep(6 * 3600)


def start_refresher() -> None:
    if settings.sites_refresh_days > 0:
        threading.Thread(target=refresher_loop, name="sites-refresher", daemon=True).start()
