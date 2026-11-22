import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import Base, SessionLocal, engine
from .models import Role, User
from .routes import admin, auth, dashboard, outings, pwa, sites
from .security import RedirectTo, hash_password, verify_csrf
from .services import sites_import
from .web import BASE, render

log = logging.getLogger("app")


def bootstrap_superadmin() -> None:
    """Crée le premier super administrateur depuis l'environnement s'il n'en existe aucun."""
    if not (settings.superadmin_email and settings.superadmin_password):
        return
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.role == Role.SUPER_ADMIN.value).limit(1)):
            return
        db.add(User(
            email=settings.superadmin_email,
            name=settings.superadmin_name,
            password_hash=hash_password(settings.superadmin_password),
            role=Role.SUPER_ADMIN.value,
        ))
        db.commit()
        log.warning("Super administrateur %s créé", settings.superadmin_email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    bootstrap_superadmin()
    if settings.env != "test":
        sites_import.start_refresher()
    yield


app = FastAPI(
    title="Climbing Planner",
    lifespan=lifespan,
    dependencies=[Depends(verify_csrf)],
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="climbing_session",
    max_age=14 * 24 * 3600,
    same_site="lax",
    https_only=settings.cookie_secure,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

for module in (pwa, auth, dashboard, sites, outings, admin):
    app.include_router(module.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return PlainTextResponse("ok")


@app.exception_handler(RedirectTo)
async def redirect_handler(request: Request, exc: RedirectTo):
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    messages = {403: "Accès refusé.", 404: "Page introuvable.", 429: "Trop de tentatives."}
    return render(request, "error.html", exc.status_code, code=exc.status_code,
                  message=messages.get(exc.status_code, str(exc.detail)))
