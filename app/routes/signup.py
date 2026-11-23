"""Inscription des membres par lien sécurisé (créé et partagé par un administrateur du club)."""
import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from ..audit import audit
from ..db import SessionLocal, get_db
from ..logging_setup import mask_email
from ..models import Club, Role, SignupLink, User, utcnow
from ..security import Throttle, admin_required, hash_password, password_error, session_epoch
from ..services import crypto, mailer, people, smtp_mailer
from ..web import flash, render
from .favorites import managed_club

router = APIRouter()
by_ip = Throttle(limit=5, window=3600)          # inscriptions par IP et par heure
VALIDITY = {"7": 7, "30": 30, "90": 90, "365": 365}
STATE_LABELS = {"active": "Actif", "revoked": "Révoqué", "expired": "Expiré", "full": "Complet"}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def link_url(request: Request, raw: str) -> str:
    return mailer.link(f"/rejoindre/{raw}") or f"{str(request.base_url).rstrip('/')}/rejoindre/{raw}"


def _valid_link(db: Session, raw: str) -> SignupLink | None:
    if not raw or len(raw) > 100:
        return None
    link = db.scalar(select(SignupLink).options(joinedload(SignupLink.club)).where(SignupLink.token_hash == _hash(raw)))
    return link if link and link.state(utcnow()) == "active" else None


# --- Administration des liens -------------------------------------------------------

def _links_page(request: Request, user: User, db: Session, club: Club | None, status_code: int = 200, **extra):
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    rows = []
    if club:
        now = utcnow()
        for link in db.scalars(select(SignupLink).where(SignupLink.club_id == club.id).order_by(SignupLink.created_at.desc())):
            raw = crypto.decrypt(link.token_enc)
            rows.append({"link": link, "state": link.state(now), "url": link_url(request, raw) if raw else None})
    return render(request, "admin_signup_links.html", status_code, club=club, clubs=clubs, rows=rows,
                  labels=STATE_LABELS, **extra)


@router.get("/admin/signup-links")
def links_page(request: Request, club: int | None = None, user: User = Depends(admin_required), db: Session = Depends(get_db)):
    cid = club if user.is_super else user.club_id
    return _links_page(request, user, db, db.get(Club, cid) if cid else None)


@router.post("/admin/signup-links")
def link_create(request: Request, club_id: str = Form(""), label: str = Form(""), validity: str = Form("30"),
                max_uses: str = Form(""), approval: str = Form(""), user: User = Depends(admin_required),
                db: Session = Depends(get_db)):
    club = managed_club(user, db, club_id)
    label = " ".join(label.split())
    try:
        days = VALIDITY[validity]
        limit = int(max_uses) if max_uses.strip() else None
        if limit is not None and not 1 <= limit <= 10000:
            raise ValueError
        if len(label) > 80:
            raise ValueError
    except (KeyError, ValueError):
        return _links_page(request, user, db, club, 400, error="Paramètres du lien invalides.")
    raw = secrets.token_urlsafe(24)          # 192 bits : impossible à deviner
    link = SignupLink(club_id=club.id, token_hash=_hash(raw), token_enc=crypto.encrypt(raw), label=label or None,
                      requires_approval=approval == "1", max_uses=limit, expires_at=utcnow() + timedelta(days=days),
                      created_by=user.id)
    db.add(link)
    db.commit()
    audit("signup_link.created", request, club_id=club.id, target=f"link:{link.id}",
          detail=f"{days} j, max={limit or '∞'}, validation={'oui' if link.requires_approval else 'non'}")
    return _links_page(request, user, db, club, created=link_url(request, raw))


@router.post("/admin/signup-links/{link_id}/revoke")
def link_revoke(link_id: int, request: Request, club_id: str = Form(""), user: User = Depends(admin_required),
                db: Session = Depends(get_db)):
    club = managed_club(user, db, club_id)
    link = db.get(SignupLink, link_id)
    if link is None or link.club_id != club.id:
        raise HTTPException(404)
    if link.revoked_at is None:
        link.revoked_at = utcnow()
        db.commit()
        audit("signup_link.revoked", request, club_id=club.id, target=f"link:{link.id}")
    flash(request, "Lien révoqué : il ne permet plus de s'inscrire.", "ok")
    return RedirectResponse(f"/admin/signup-links?club={club.id}" if user.is_super else "/admin/signup-links", status_code=303)


# --- Validation des inscriptions en attente -------------------------------------------------

def _pending_target(db: Session, actor: User, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None or not target.pending or not (actor.is_super or target.club_id == actor.club_id):
        raise HTTPException(404)
    return target


def _notify_member(club_id: int, email: str, name: str, subject: str, paragraphs: list[str], cta: tuple | None) -> None:
    """Mailjet du club s'il est configuré, sinon SMTP. Ne lève jamais."""
    text, html = mailer.build(subject, name, paragraphs, cta)
    mail = mailer.Mail(email, name, subject, text, html)
    with SessionLocal() as db:
        cfg = mailer.load_config(db, club_id)
    if cfg is not None:
        mailer.send(cfg, [mail])
    elif smtp_mailer.configured():
        smtp_mailer.send(mail)


@router.post("/admin/users/{user_id}/approve")
def signup_approve(user_id: int, request: Request, background: BackgroundTasks, user: User = Depends(admin_required),
                   db: Session = Depends(get_db)):
    target = _pending_target(db, user, user_id)
    target.pending, target.is_active = False, True
    db.commit()
    audit("user.signup_approved", request, club_id=target.club_id, target=f"user:{target.id}")
    club = db.get(Club, target.club_id)
    background.add_task(_notify_member, target.club_id, target.email, target.name, f"Bienvenue à {club.name}",
                        [f"Votre inscription à {club.name} sur Climbing Planner est validée. Vous pouvez vous connecter."],
                        ("Se connecter", mailer.link("/login")) if mailer.link("/login") else None)
    flash(request, f"Inscription de {target.name} validée.", "ok")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/reject")
def signup_reject(user_id: int, request: Request, user: User = Depends(admin_required), db: Session = Depends(get_db)):
    target = _pending_target(db, user, user_id)
    name, club_id = target.name, target.club_id
    db.delete(target)            # jamais connecté : aucune donnée rattachée
    db.commit()
    audit("user.signup_rejected", request, club_id=club_id, target=f"user:{user_id}")
    flash(request, f"Inscription de {name} refusée et supprimée.", "ok")
    return RedirectResponse("/admin/users", status_code=303)


# --- Page publique : /rejoindre/<jeton> --------------------------------------------------------

@router.get("/rejoindre/{token}")
def join_form(token: str, request: Request, db: Session = Depends(get_db)):
    link = _valid_link(db, token)
    if link is None:
        return render(request, "join.html", 404, invalid=True)
    return render(request, "join.html", club=link.club, token=token, link=link, form={})


@router.post("/rejoindre/{token}")
def join_submit(token: str, request: Request, background: BackgroundTasks, first_name: str = Form(""),
                last_name: str = Form(""), email: str = Form(""), phone: str = Form(""), password: str = Form(""),
                confirm: str = Form(""), consent: str = Form(""), website: str = Form(""), db: Session = Depends(get_db)):
    link = _valid_link(db, token)
    if link is None:
        return render(request, "join.html", 404, invalid=True)
    form = dict(first_name=first_name, last_name=last_name, email=email, phone=phone)
    ip = request.client.host if request.client else "?"

    def fail(msg: str, code: int = 400):
        return render(request, "join.html", code, club=link.club, token=token, link=link, form=form, error=msg)

    if website:                                  # champ piège : robot
        return render(request, "join.html", club=link.club, done="pending")
    if by_ip.blocked(ip):
        return fail("Trop d'inscriptions depuis cette connexion. Réessayez plus tard.", 429)
    try:
        first, last, mail, tel = people.validate_person(first_name, last_name, email, phone)
        if not (first and last):
            raise people.ValidationError("Indiquez votre prénom et votre nom.")
    except people.ValidationError as exc:
        return fail(str(exc))
    if password != confirm:
        return fail("Les deux mots de passe ne correspondent pas.")
    if (err := password_error(password)):
        return fail(err)
    if consent != "1":
        return fail("Merci d'accepter l'utilisation de vos données par le club.")
    if db.scalar(select(User.id).where(User.email == mail)):
        return fail("Un compte existe déjà avec cette adresse : connectez-vous, ou utilisez « Mot de passe oublié ».")
    # Consommation atomique d'une utilisation (deux inscriptions simultanées ne dépassent pas la limite)
    q = update(SignupLink).where(SignupLink.id == link.id, SignupLink.revoked_at.is_(None))
    if link.max_uses is not None:
        q = q.where(SignupLink.uses < link.max_uses)
    if db.execute(q.values(uses=SignupLink.uses + 1)).rowcount != 1:
        db.rollback()
        return render(request, "join.html", 404, invalid=True)
    by_ip.add(ip)
    pending = link.requires_approval
    member = User(email=mail, first_name=first, last_name=last, phone=tel, name=f"{first} {last}",
                  role=Role.MEMBER.value, club_id=link.club_id, password_hash=hash_password(password),
                  is_active=not pending, pending=pending)
    db.add(member)
    db.commit()
    audit("user.signup", request, actor=member, club_id=link.club_id, target=f"user:{member.id}",
          detail=f"lien {link.id}, {'en attente' if pending else 'actif'}")
    if pending:
        admins = db.scalars(select(User).where(User.club_id == link.club_id, User.role == Role.CLUB_ADMIN.value,
                                               User.is_active.is_(True))).all()
        url = mailer.link("/admin/users")
        for a in admins:
            background.add_task(_notify_member, link.club_id, a.email, a.name, "Nouvelle inscription à valider",
                                [f"{member.name} s'est inscrit(e) au club avec un lien d'inscription."],
                                ("Valider l'inscription", url) if url else None)
        return render(request, "join.html", club=link.club, done="pending")
    request.session.clear()                      # compte actif : connexion directe
    request.session["uid"] = member.id
    request.session["ep"] = session_epoch(db, member.id)
    flash(request, f"Bienvenue à {link.club.name} !", "ok")
    return RedirectResponse("/", status_code=303)
