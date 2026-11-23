import re

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..models import ROLE_LABELS, Club, ClubFavorite, ClubIntegration, Membership, Outing, Role, Site, SiteImport, User
from ..security import (
    admin_required, generate_password, hash_password, password_error, super_required,
)
from ..services import memberships, people, notifications, sites_import, tokens, mailer
from .dashboard import today
from ..web import flash, render

router = APIRouter(prefix="/admin")


# --- Clubs (super administrateur) -------------------------------------------

@router.get("/clubs")
def clubs_page(request: Request, user: User = Depends(super_required), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Club, func.count(User.id)).outerjoin(User, User.club_id == Club.id)
        .group_by(Club.id).order_by(Club.name)
    ).all()
    return render(request, "admin_clubs.html", rows=rows)


@router.post("/clubs")
def club_create(request: Request, name: str = Form(...), user: User = Depends(super_required),
                db: Session = Depends(get_db)):
    name = " ".join(name.split())[:120]
    if not name:
        flash(request, "Nom de club obligatoire.", "error")
    elif name_taken(db, name):
        flash(request, "Ce club existe déjà.", "error")
    else:
        try:
            club = Club(name=name)
            db.add(club)
            db.commit()
            audit("club.created", request, club_id=club.id, target=f"club:{club.id}", detail=name)
            flash(request, f"Club « {name} » créé.", "ok")
        except IntegrityError:
            db.rollback()
            flash(request, "Ce club existe déjà.", "error")
    return RedirectResponse("/admin/clubs", status_code=303)


def name_taken(db: Session, name: str, except_id: int | None = None) -> bool:
    """Nom de club déjà utilisé (sans tenir compte de la casse)."""
    q = select(Club.id).where(func.lower(Club.name) == name.lower())
    if except_id is not None:
        q = q.where(Club.id != except_id)
    return db.scalar(q) is not None


def _club_summary(db: Session, club: Club) -> dict:
    users = db.scalars(select(User).where(User.club_id == club.id).order_by(User.last_name, User.first_name)).all()
    integ = db.get(ClubIntegration, club.id)
    return dict(
        members=len(users), active=sum(1 for u in users if u.is_active),
        admins=[u for u in users if u.role == Role.CLUB_ADMIN.value],
        outings=db.scalar(select(func.count()).select_from(Outing).where(Outing.club_id == club.id)),
        upcoming=db.scalar(select(func.count()).select_from(Outing).where(Outing.club_id == club.id, Outing.day >= today())),
        favorites=db.scalar(select(func.count()).select_from(ClubFavorite).where(ClubFavorite.club_id == club.id)),
        mailjet=bool(integ and integ.mailjet_ready), helloasso=bool(integ and integ.helloasso_ready))


@router.get("/clubs/{club_id}/edit")
def club_edit_form(club_id: int, request: Request, user: User = Depends(super_required), db: Session = Depends(get_db)):
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(404)
    return render(request, "admin_club_edit.html", club=club, summary=_club_summary(db, club))


@router.post("/clubs/{club_id}/edit")
def club_edit(club_id: int, request: Request, name: str = Form(""), user: User = Depends(super_required),
              db: Session = Depends(get_db)):
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(404)
    new = " ".join(name.split())
    error = None
    if not 2 <= len(new) <= 120:
        error = "Le nom doit contenir entre 2 et 120 caractères."
    elif name_taken(db, new, except_id=club.id):
        error = "Un autre club porte déjà ce nom."
    if error:
        return render(request, "admin_club_edit.html", 400, club=club, summary=_club_summary(db, club),
                      error=error, form_name=name)
    old = club.name
    if new != old:
        club.name = new
        db.commit()
        audit("club.renamed", request, club_id=club.id, target=f"club:{club.id}", detail=f"{old} -> {new}")
        flash(request, f"Club renommé en « {new} ».", "ok")
    return RedirectResponse(f"/admin/clubs/{club.id}/edit", status_code=303)


@router.post("/clubs/{club_id}/delete")
def club_delete(club_id: int, request: Request, user: User = Depends(super_required),
                db: Session = Depends(get_db)):
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(404)
    has_data = db.scalar(select(func.count()).select_from(User).where(User.club_id == club_id)) \
        or db.scalar(select(func.count()).select_from(Outing).where(Outing.club_id == club_id))
    if has_data:
        flash(request, "Impossible : ce club a encore des utilisateurs ou des sorties.", "error")
    else:
        # Données rattachées au club (clés étrangères) : sinon PostgreSQL refuse la suppression
        for model in (ClubFavorite, Membership):
            for row in db.scalars(select(model).where(model.club_id == club_id)):
                db.delete(row)
        integ = db.get(ClubIntegration, club_id)
        if integ:
            db.delete(integ)
        db.delete(club)
        db.commit()
        audit("club.deleted", request, club_id=club_id, target=f"club:{club_id}", detail=club.name)
        flash(request, "Club supprimé.", "ok")
    return RedirectResponse("/admin/clubs", status_code=303)


# --- Utilisateurs -----------------------------------------------------------

def _manageable(actor: User, target: User) -> bool:
    """Un admin de club ne gère que les membres/admins de son propre club."""
    if target.id == actor.id:
        return False
    if actor.is_super:
        return True
    return target.club_id == actor.club_id and not target.is_super


PAGE = 50


def users_query(actor: User, q: str = "", club: int | None = None):
    """Utilisateurs visibles par `actor` (un administrateur de club ne voit que son club)."""
    stmt = select(User)
    if actor.is_super:
        if club:
            stmt = stmt.where(User.club_id == club)
    else:
        stmt = stmt.where(User.club_id == actor.club_id)
    q = q.strip()[:100]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds = [User.name.ilike(like, escape="\\"), User.email.ilike(like, escape="\\"),
                 User.phone.ilike(like, escape="\\")]
        digits = re.sub(r"[ .\-+()]", "", q)
        if len(digits) >= 3 and digits.isdigit():
            # les numéros sont stockés au format international : « 0698… » doit trouver « +33698… »
            conds.append(User.phone.like("%" + ("33" + digits[1:] if digits.startswith("0") else digits) + "%"))
        stmt = stmt.where(or_(*conds))
    return stmt


def dues_for(db: Session, club_ids) -> dict[str, bool]:
    dues: dict[str, bool] = {}
    for cid in {c for c in club_ids if c}:
        integ = db.get(ClubIntegration, cid)
        if integ and integ.ha_membership_form:
            dues.update({e: ok for e, (_, ok) in memberships.status_by_email(db, cid, today()).items()})
    return dues


def _users_context(db: Session, actor: User, q: str = "", club: int | None = None, page: int = 1, **extra):
    stmt = users_query(actor, q, club)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    page = max(page, 1)
    users = db.scalars(stmt.order_by(User.club_id, func.lower(func.coalesce(User.last_name, User.name)),
                                     func.lower(func.coalesce(User.first_name, "")))
                       .offset((page - 1) * PAGE).limit(PAGE)).all()
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if actor.is_super else []
    names = {c.id: c.name for c in db.scalars(select(Club))}
    roles = ROLE_LABELS if actor.is_super else {k: v for k, v in ROLE_LABELS.items()
                                                if k != Role.SUPER_ADMIN.value}
    return dict(users=users, clubs=clubs, club_names=names, roles=roles, dues=dues_for(db, [u.club_id for u in users]),
                manageable={u.id: _manageable(actor, u) for u in users},
                mail_ok=bool(actor.club_id and mailer.load_config(db, actor.club_id)),
                q=q.strip()[:100], club_filter=club, total=total, page=page, pages=max(1, -(-total // PAGE)), **extra)


@router.get("/users")
def users_page(request: Request, q: str = "", club: int | None = None, page: int = 1,
               user: User = Depends(admin_required), db: Session = Depends(get_db)):
    return render(request, "admin_users.html", **_users_context(db, user, q, club, page))


@router.post("/users")
def user_create(request: Request, first_name: str = Form(""), last_name: str = Form(""), email: str = Form(...),
                phone: str = Form(""), role: str = Form(Role.MEMBER.value), club_id: str = Form(""),
                password: str = Form(""), invite: str = Form(""), user: User = Depends(admin_required),
                db: Session = Depends(get_db)):
    error = None
    try:
        first_name, last_name, email, phone = people.validate_person(first_name, last_name, email, phone)
    except people.ValidationError as exc:
        error = str(exc)
    allowed = ROLE_LABELS if user.is_super else {k: v for k, v in ROLE_LABELS.items()
                                                 if k != Role.SUPER_ADMIN.value}
    if not error and role not in allowed:
        error = "Rôle invalide."
    if user.is_super:
        target_club = int(club_id) if club_id.isdigit() else None
        if role == Role.SUPER_ADMIN.value:
            target_club = None
        elif target_club is None or db.get(Club, target_club) is None:
            error = error or "Choisissez un club."
    else:
        target_club = user.club_id
    temp = password or generate_password()
    if not error and password:
        error = password_error(password)
    if not error and db.scalar(select(User.id).where(User.email == email)):
        error = "Cet email est déjà utilisé."
    if error:
        return render(request, "admin_users.html", 400, error=error, **_users_context(db, user))
    new_user = User(email=email, first_name=first_name, last_name=last_name, phone=phone, role=role, club_id=target_club,
                    password_hash=hash_password(temp), must_change_password=True)
    db.add(new_user)
    db.commit()
    audit("user.created", request, club_id=target_club, target=f"user:{new_user.id}", detail=f"role={role}")
    if invite == "1" and not password and target_club and mailer.load_config(db, target_club):
        new_user.must_change_password = False   # la personne choisit son mot de passe via le lien
        db.commit()
        ok, err = notifications.send_invite(db, new_user)
        if ok:
            return render(request, "admin_users.html", **_users_context(db, user, invited=email))
        # échec d'envoi : on retombe sur le mot de passe provisoire à transmettre à la main
        new_user.must_change_password = True
        db.commit()
        return render(request, "admin_users.html", **_users_context(
            db, user, credentials=dict(email=email, password=temp), invite_error=err))
    return render(request, "admin_users.html", **_users_context(
        db, user, credentials=dict(email=email, password=temp)))


def _target(db: Session, actor: User, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None or not (actor.is_super or target.club_id == actor.club_id):
        raise HTTPException(404)
    if not _manageable(actor, target):
        raise HTTPException(403, "Vous ne pouvez pas modifier cet utilisateur.")
    return target


@router.post("/users/{user_id}")
def user_update(user_id: int, request: Request, role: str = Form(...), active: str = Form(""),
                user: User = Depends(admin_required), db: Session = Depends(get_db)):
    target = _target(db, user, user_id)
    # Le rôle super administrateur ne se modifie pas ici (il n'a pas de club)
    if role in (Role.CLUB_ADMIN.value, Role.MEMBER.value) and not target.is_super:
        target.role = role
    target.is_active = active == "1"
    db.commit()
    audit("user.updated", request, club_id=target.club_id, target=f"user:{target.id}",
          detail=f"role={target.role} active={target.is_active}")
    flash(request, f"{target.name} mis à jour.", "ok")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/reset")
def user_reset(user_id: int, request: Request, user: User = Depends(admin_required),
               db: Session = Depends(get_db)):
    target = _target(db, user, user_id)
    temp = generate_password()
    target.password_hash = hash_password(temp)
    target.must_change_password = True
    tokens.bump_epoch(db, target.id)   # déconnecte ses sessions ouvertes
    db.commit()
    audit("user.password_reset_by_admin", request, club_id=target.club_id, target=f"user:{target.id}")
    return render(request, "admin_users.html", **_users_context(
        db, user, credentials=dict(email=target.email, password=temp)))


# --- Import des sites (super administrateur) --------------------------------

@router.get("/sites")
def sites_admin(request: Request, user: User = Depends(super_required), db: Session = Depends(get_db)):
    imports = db.scalars(select(SiteImport).order_by(SiteImport.id.desc()).limit(10)).all()
    total = db.scalar(select(func.count()).select_from(Site))
    return render(request, "admin_sites.html", imports=imports, total=total,
                  running=sites_import.is_running())


@router.post("/sites/import")
def sites_import_start(request: Request, background: BackgroundTasks,
                       user: User = Depends(super_required)):
    if sites_import.is_running():
        flash(request, "Un import est déjà en cours.", "error")
    else:
        audit("sites.import_started", request)
        background.add_task(sites_import.run_import)
        flash(request, "Import lancé : cela peut prendre quelques minutes.", "ok")
    return RedirectResponse("/admin/sites", status_code=303)
