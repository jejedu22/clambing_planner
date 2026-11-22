"""Fichiers PWA servis à la racine (le service worker doit couvrir tout le site)."""
import hashlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..web import BASE, render

router = APIRouter()
STATIC = BASE / "static"


def _version() -> str:
    """Empreinte des fichiers statiques : change à chaque déploiement modifiant les assets."""
    h = hashlib.sha256()
    for p in sorted(STATIC.rglob("*")):
        if p.is_file() and p.name != "sw.js":
            h.update(p.relative_to(STATIC).as_posix().encode())
            h.update(p.read_bytes())
    for p in sorted((BASE / "templates").glob("offline.html")):
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


VERSION = _version()
MANIFEST = {
    "name": "Climbing Planner",
    "short_name": "Climbing",
    "description": "Organisez les sorties d'escalade de votre club : sites, météo, participants.",
    "lang": "fr",
    "start_url": "/",
    "scope": "/",
    "id": "/",
    "display": "standalone",
    "background_color": "#f6f4ef",
    "theme_color": "#c2540a",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
    "shortcuts": [
        {"name": "Sorties", "url": "/outings"},
        {"name": "Sites d'escalade", "url": "/sites"},
    ],
}


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    body = (STATIC / "sw.js").read_text().replace("__VERSION__", VERSION)
    return Response(body, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@router.get("/offline", include_in_schema=False)
def offline(request: Request):
    return render(request, "offline.html")
