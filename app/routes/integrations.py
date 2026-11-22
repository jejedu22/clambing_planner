"""Intégrations par club (Mailjet, HelloAsso) et adhésions HelloAsso."""
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..models import Club, ClubIntegration, Membership, Role, User
from ..security import admin_required, hash_password, generate_password
from ..services import crypto, mailer, memberships, notifications
from ..services.helloasso import SLUG_RE, HelloAssoError
from ..web import flash, render
from .dashboard import today

router = APIRouter(prefix="/admin")

KEY_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
HA_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{4,120}$")
SECRET_RE = re.compile(r"^\S{4,200}$")


def _club(user: User, db: Session, club_id: int | None) -> Club | None:
    """Club géré : celui de l'administrateur ; le super administrateur choisit."""
    cid = club_id if user.is_super else user.club_id
    return db.get(Club, cid) if cid else None


def _need_club(user: User, db: Session, club_id: int | None) -> Club:
    club = _club(user, db, club_id)
    if club is None:
        raise HTTPException(404, "Club introuvable.")
    return club


def _integ(db: Session, club_id: int) -> ClubIntegration:
    integ = db.get(ClubIntegration, club_id)
    if integ is None:
        integ = ClubIntegration(club_id=club_id)
        db.add(integ)
    return integ


def _back(club: Club, user: User) -> RedirectResponse:
    return RedirectResponse(f"/admin/integrations?club={club.id}" if user.is_super else "/admin/integrations",
                            status_code=303)


def _page(request: Request, user: User, db: Session, club: Club | None, status_code: int = 200, **extra):
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    integ = db.get(ClubIntegration, club.id) if club else None
    return render(request, "admin_integrations.html", status_code, club=club, clubs=clubs, integ=integ,
                  base_url_ok=bool(mailer.link("/")), **extra)


@router.get("/integrations")
def integrations_page(request: Request, club: int | None = None, user: User = Depends(admin_required),
                      db: Session = Depends(get_db)):
    return _page(request, user, db, _club(user, db, club))


# --- Mailjet ----------------------------------------------------------------

@router.post("/integrations/mailjet")
def mailjet_save(request: Request, club_id: int | None = Form(None), api_key: str = Form(""),
                 api_secret: str = Form(""), from_email: str = Form(""), from_name: str = Form(""),
                 user: User = Depends(admin_required), db: Session = Depends(get_db)):
    club = _need_club(user, db, club_id)
    integ = _integ(db, club.id)
    api_key, api_secret, from_email = api_key.strip(), api_secret.strip(), from_email.strip()
    error = None
    if not KEY_RE.match(api_key):
        error = "Clé API Mailjet invalide."
    elif api_secret and not KEY_RE.match(api_secret):
        error = "Clé secrète Mailjet invalide."
    elif not api_secret and not integ.mj_api_secret_enc:
        error = "La clé secrète est obligatoire."
    elif not mailer.EMAIL_RE.match(from_email):
        error = "Adresse d'expéditeur invalide."
    if error:
        db.rollback()
        return _page(request, user, db, club, 400, error=error)
    integ.mj_api_key, integ.mj_from_email = api_key, from_email
    integ.mj_from_name = " ".join(from_name.split())[:120] or club.name
    if api_secret:
        integ.mj_api_secret_enc = crypto.encrypt(api_secret)
    db.commit()
    audit("integration.mailjet_saved", request, club_id=club.id, target=f"club:{club.id}", detail="secret mis à jour" if api_secret else None)
    flash(request, "Configuration Mailjet enregistrée.", "ok")
    return _back(club, user)


@router.post("/integrations/mailjet/check")
def mailjet_check(request: Request, club_id: int | None = Form(None), user: User = Depends(admin_required),
                  db: Session = Depends(get_db)):
    """Valide clés et expéditeur en mode bac à sable : aucun email n'est réellement envoyé."""
    club = _need_club(user, db, club_id)
    cfg = mailer.load_config(db, club.id)
    if cfg is None:
        flash(request, "Enregistrez d'abord la configuration Mailjet.", "error")
    else:
        probe = mailer.Mail(user.email, user.name, "Test", "Test", "<p>Test</p>")
        sent, err = mailer.send(cfg, [probe], sandbox=True)
        flash(request, "Clés et expéditeur valides (aucun email envoyé)." if sent and not err else
              f"Vérification échouée : {err}", "ok" if sent and not err else "error")
    return _back(club, user)


@router.post("/integrations/mailjet/test")
def mailjet_test(request: Request, club_id: int | None = Form(None), user: User = Depends(admin_required),
                 db: Session = Depends(get_db)):
    """Envoie un vrai email de test à l'administrateur connecté (et à lui seul)."""
    club = _need_club(user, db, club_id)
    cfg = mailer.load_config(db, club.id)
    if cfg is None:
        flash(request, "Enregistrez d'abord la configuration Mailjet.", "error")
    else:
        text, html = mailer.build("Email de test", user.name,
                                  ["Si vous lisez ce message, l'envoi d'emails de Climbing Planner fonctionne."])
        sent, err = mailer.send(cfg, [mailer.Mail(user.email, user.name, "Email de test — Climbing Planner", text, html)])
        flash(request, f"Email de test envoyé à {user.email}." if sent and not err else
              f"Échec de l'envoi : {err}", "ok" if sent and not err else "error")
    return _back(club, user)


@router.post("/integrations/mailjet/delete")
def mailjet_delete(request: Request, club_id: int | None = Form(None), user: User = Depends(admin_required),
                   db: Session = Depends(get_db)):
    club = _need_club(user, db, club_id)
    integ = _integ(db, club.id)
    integ.mj_api_key = integ.mj_api_secret_enc = integ.mj_from_email = integ.mj_from_name = None
    db.commit()
    audit("integration.mailjet_deleted", request, club_id=club.id, target=f"club:{club.id}")
    flash(request, "Configuration Mailjet supprimée.", "ok")
    return _back(club, user)


# --- HelloAsso --------------------------------------------------------------

@router.post("/integrations/helloasso")
def helloasso_save(request: Request, club_id: int | None = Form(None), client_id: str = Form(""),
                   client_secret: str = Form(""), org_slug: str = Form(""),
                   user: User = Depends(admin_required), db: Session = Depends(get_db)):
    club = _need_club(user, db, club_id)
    integ = _integ(db, club.id)
    client_id, client_secret, org_slug = client_id.strip(), client_secret.strip(), org_slug.strip().lower()
    error = None
    if not HA_ID_RE.match(client_id):
        error = "Client ID HelloAsso invalide."
    elif client_secret and not SECRET_RE.match(client_secret):
        error = "Client secret invalide."
    elif not client_secret and not integ.ha_client_secret_enc:
        error = "Le client secret est obligatoire."
    elif not SLUG_RE.match(org_slug):
        error = "Identifiant (slug) d'organisation invalide : par ex. « mon-club-d-escalade »."
    if error:
        db.rollback()
        return _page(request, user, db, club, 400, error=error)
    if org_slug != integ.ha_org_slug:
        integ.ha_membership_form = None   # un autre compte : l'ancien formulaire n'a plus de sens
    integ.ha_client_id, integ.ha_org_slug = client_id, org_slug
    if client_secret:
        integ.ha_client_secret_enc = crypto.encrypt(client_secret)
    db.commit()
    audit("integration.helloasso_saved", request, club_id=club.id, target=f"club:{club.id}", detail="secret mis à jour" if client_secret else None)
    flash(request, "Configuration HelloAsso enregistrée.", "ok")
    return _back(club, user)


@router.post("/integrations/helloasso/check")
def helloasso_check(request: Request, club_id: int | None = Form(None), user: User = Depends(admin_required),
                    db: Session = Depends(get_db)):
    club = _need_club(user, db, club_id)
    integ = db.get(ClubIntegration, club.id)
    try:
        client = memberships.client_for(integ)
        forms = client.forms(integ.ha_org_slug)
    except HelloAssoError as exc:
        return _page(request, user, db, club, 400, error=str(exc))
    return _page(request, user, db, club, ha_forms=[f for f in forms if f["type"] == "Membership"],
                 ha_ok=f"Connexion réussie : {len(forms)} formulaire(s) trouvé(s).")


@router.post("/integrations/helloasso/membership")
def helloasso_membership(request: Request, club_id: int | None = Form(None), form_slug: str = Form(""),
                         mode: str = Form("season"), user: User = Depends(admin_required),
                         db: Session = Depends(get_db)):
    club = _need_club(user, db, club_id)
    integ = _integ(db, club.id)
    form_slug = form_slug.strip().lower()
    if form_slug and not SLUG_RE.match(form_slug):
        db.rollback()
        return _page(request, user, db, club, 400, error="Slug de formulaire invalide.")
    integ.ha_membership_form = form_slug or None
    integ.membership_mode = mode if mode in ("season", "rolling") else "season"
    db.commit()
    flash(request, "Réglages des adhésions enregistrés.", "ok")
    return _back(club, user)


@router.post("/integrations/helloasso/delete")
def helloasso_delete(request: Request, club_id: int | None = Form(None), user: User = Depends(admin_required),
                     db: Session = Depends(get_db)):
    club = _need_club(user, db, club_id)
    integ = _integ(db, club.id)
    integ.ha_client_id = integ.ha_client_secret_enc = integ.ha_org_slug = integ.ha_membership_form = None
    integ.ha_synced_at = None
    for m in db.scalars(select(Membership).where(Membership.club_id == club.id)):
        db.delete(m)
    db.commit()
    audit("integration.helloasso_deleted", request, club_id=club.id, target=f"club:{club.id}")
    flash(request, "Configuration HelloAsso et adhésions importées supprimées.", "ok")
    return _back(club, user)


# --- Adhésions ---------------------------------------------------------------

def _memberships_page(request: Request, user: User, db: Session, club: Club | None, status_code: int = 200, **extra):
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    rows, accounts, integ = [], {}, None
    if club:
        integ = db.get(ClubIntegration, club.id)
        status = memberships.status_by_email(db, club.id, today())
        rows = sorted(status.values(), key=lambda r: r[0].full_name.lower())
        for u in db.scalars(select(User).where(User.email.in_([m.email for m, _ in rows]))):
            accounts[u.email] = "yes" if u.club_id == club.id else "other"
    return render(request, "admin_memberships.html", status_code, club=club, clubs=clubs, integ=integ,
                  rows=rows, accounts=accounts, mail_ok=bool(club and mailer.load_config(db, club.id)), **extra)


@router.get("/memberships")
def memberships_page(request: Request, club: int | None = None, user: User = Depends(admin_required),
                     db: Session = Depends(get_db)):
    return _memberships_page(request, user, db, _club(user, db, club))


@router.post("/memberships/sync")
def memberships_sync(request: Request, club_id: int | None = Form(None), user: User = Depends(admin_required),
                     db: Session = Depends(get_db)):
    club = _need_club(user, db, club_id)
    try:
        n = memberships.sync(db, club.id)
    except HelloAssoError as exc:
        return _memberships_page(request, user, db, club, 400, error=str(exc))
    audit("memberships.synced", request, club_id=club.id, target=f"club:{club.id}", detail=f"{n} adhésion(s)")
    flash(request, f"{n} adhésion(s) synchronisée(s).", "ok")
    return RedirectResponse(f"/admin/memberships?club={club.id}" if user.is_super else "/admin/memberships",
                            status_code=303)


@router.post("/memberships/invite")
def memberships_invite(request: Request, club_id: int | None = Form(None), email: str = Form(""),
                       user: User = Depends(admin_required), db: Session = Depends(get_db)):
    """Crée le compte membre (un email ou « tous »). Invitation par email si Mailjet est configuré."""
    club = _need_club(user, db, club_id)
    status = memberships.status_by_email(db, club.id, today())
    wanted = list(status) if email == "__all__" else [email.strip().lower()]
    mail_ok = mailer.load_config(db, club.id) is not None
    created, skipped, credentials, errors = 0, 0, [], []
    for addr in wanted:
        if addr not in status:
            continue   # on ne crée de compte que pour une adhésion réellement importée
        if db.scalar(select(User.id).where(User.email == addr)):
            skipped += 1
            continue
        m = status[addr][0]
        temp = generate_password()
        member = User(email=addr, first_name=m.first_name[:55] or None, last_name=m.last_name[:55] or None,
                      name=m.full_name[:120], role=Role.MEMBER.value, club_id=club.id,
                      password_hash=hash_password(temp), must_change_password=not mail_ok)
        db.add(member)
        db.commit()
        created += 1
        if mail_ok:
            ok, err = notifications.send_invite(db, member)
            if not ok:
                errors.append(f"{addr} : {err}")
        else:
            credentials.append({"email": addr, "password": temp})
    if created:
        audit("memberships.accounts_created", request, club_id=club.id, target=f"club:{club.id}", detail=f"{created} compte(s)")
    return _memberships_page(request, user, db, club, created=created, skipped=skipped,
                             credentials=credentials, mail_errors=errors)
