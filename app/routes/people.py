"""Fiche utilisateur : modification, export / import CSV, profil personnel."""
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..logging_setup import mask_email
from ..models import ROLE_LABELS, AuthToken, Club, Role, User, utcnow
from ..security import admin_required, login_required
from ..services import mailer, notifications, people
from ..web import flash, render
from .admin import _manageable, dues_for, users_query

router = APIRouter()


def _roles_for(actor: User) -> dict[str, str]:
    return ROLE_LABELS if actor.is_super else {k: v for k, v in ROLE_LABELS.items() if k != Role.SUPER_ADMIN.value}


def _target(db: Session, actor: User, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None or not (actor.is_super or target.club_id == actor.club_id):
        raise HTTPException(404)          # 404 : on ne révèle pas les comptes des autres clubs
    if not _manageable(actor, target):
        raise HTTPException(403, "Vous ne pouvez pas modifier cet utilisateur.")
    return target


# --- Modification complète -----------------------------------------------------

@router.get("/admin/users/{user_id}/edit")
def user_edit_form(user_id: int, request: Request, user: User = Depends(admin_required), db: Session = Depends(get_db)):
    target = _target(db, user, user_id)
    return render(request, "admin_user_edit.html", target=target, roles=_roles_for(user))


@router.post("/admin/users/{user_id}/edit")
def user_edit(user_id: int, request: Request, first_name: str = Form(""), last_name: str = Form(""),
              email: str = Form(""), phone: str = Form(""), role: str = Form(""), active: str = Form(""),
              user: User = Depends(admin_required), db: Session = Depends(get_db)):
    target = _target(db, user, user_id)
    try:
        first, last, new_email, new_phone = people.validate_person(first_name, last_name, email, phone)
        if new_email != target.email and db.scalar(select(User.id).where(User.email == new_email)):
            raise people.ValidationError("Cet email est déjà utilisé.")
        if not target.is_super and role not in (Role.MEMBER.value, Role.CLUB_ADMIN.value):
            raise people.ValidationError("Rôle invalide.")
    except people.ValidationError as exc:
        return render(request, "admin_user_edit.html", 400, target=target, roles=_roles_for(user), error=str(exc),
                      form=dict(first_name=first_name, last_name=last_name, email=email, phone=phone, role=role,
                                active=active == "1"))
    old_email = target.email
    target.first_name, target.last_name, target.phone = first, last, new_phone
    if not target.is_super:
        target.role = role
    target.is_active = active == "1"
    if target.is_active:
        target.pending = False
    if new_email != old_email:
        target.email = new_email
        # les liens en attente ont été envoyés à l'ancienne adresse : on les invalide
        db.execute(update(AuthToken).where(AuthToken.user_id == target.id, AuthToken.used_at.is_(None))
                   .values(used_at=utcnow()))
    db.commit()
    audit("user.updated", request, club_id=target.club_id, target=f"user:{target.id}",
          detail=f"role={target.role} active={target.is_active}")
    if new_email != old_email:
        audit("user.email_changed", request, club_id=target.club_id, target=f"user:{target.id}",
              detail=f"{mask_email(old_email)} -> {mask_email(new_email)}")
    flash(request, "Utilisateur mis à jour.", "ok")
    return RedirectResponse("/admin/users", status_code=303)


# --- Export ---------------------------------------------------------------------

@router.get("/admin/users/export")
def users_export(request: Request, q: str = "", club: int | None = None,
                 user: User = Depends(admin_required), db: Session = Depends(get_db)):
    users = db.scalars(users_query(user, q, club)).all()
    names = {c.id: c.name for c in db.scalars(select(Club))}
    data = people.export_csv(users, names, dues_for(db, [u.club_id for u in users]))
    audit("users.exported", request, club_id=user.club_id if not user.is_super else club, detail=f"{len(users)} ligne(s)")
    slug = "".join(ch if ch.isalnum() else "-" for ch in (names.get(club or user.club_id, "tous-les-clubs")).lower()).strip("-")
    return Response(data, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="utilisateurs-{slug or "club"}-{datetime.now():%Y%m%d}.csv"',
        "Cache-Control": "no-store"})   # données personnelles : jamais mises en cache


@router.get("/admin/users/import/template")
def import_template(user: User = Depends(admin_required)):
    return Response(people.TEMPLATE_CSV, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="modele-import-utilisateurs.csv"'})


# --- Import (simulation puis confirmation) -----------------------------------------

def _import_club(user: User, db: Session, club_id: str) -> Club | None:
    cid = int(club_id) if user.is_super and club_id.isdigit() else user.club_id
    return db.get(Club, cid) if cid else None


@router.get("/admin/users/import")
def import_form(request: Request, user: User = Depends(admin_required), db: Session = Depends(get_db)):
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    return render(request, "admin_users_import.html", clubs=clubs, mail_ok=_mail_ok(db, user.club_id))


def _mail_ok(db: Session, club_id: int | None) -> bool:
    return bool(club_id and mailer.load_config(db, club_id))


@router.post("/admin/users/import")
def import_submit(request: Request, background: BackgroundTasks, step: str = Form("preview"), club_id: str = Form(""),
                  invite: str = Form(""), csv_text: str = Form(""), file: UploadFile | None = File(None),
                  user: User = Depends(admin_required), db: Session = Depends(get_db)):
    clubs = db.scalars(select(Club).order_by(Club.name)).all() if user.is_super else []
    club = _import_club(user, db, club_id)

    def fail(msg: str):
        return render(request, "admin_users_import.html", 400, clubs=clubs, error=msg, club=club,
                      mail_ok=_mail_ok(db, club.id if club else user.club_id))

    if club is None:
        return fail("Choisissez un club.")
    try:
        if step == "confirm":
            text = csv_text                      # re-validé intégralement : le champ caché n'est pas de confiance
            if len(text.encode()) > people.MAX_BYTES:
                raise people.ValidationError("Fichier trop volumineux.")
        else:
            if file is None or not file.filename:
                raise people.ValidationError("Choisissez un fichier CSV.")
            text = people.decode(file.file.read(people.MAX_BYTES + 1))
        plan = people.build_plan(db, club, people.parse_csv(text))
    except people.ValidationError as exc:
        return fail(str(exc))

    counts = {a: sum(1 for r in plan if r.action == a) for a in ("create", "update", "skip", "error")}
    mail_ok = _mail_ok(db, club.id)
    if step != "confirm":
        return render(request, "admin_users_import.html", clubs=clubs, club=club, plan=plan, counts=counts,
                      csv_text=text, invite=invite == "1" and mail_ok, mail_ok=mail_ok)

    created, updated, credentials = people.apply_plan(db, club, plan)
    invited = invite == "1" and mail_ok
    if invited and created:
        for u in created:
            u.must_change_password = False        # chacun choisit son mot de passe via le lien d'invitation
        db.commit()
        background.add_task(notifications.invite_many, [u.id for u in created])
        credentials = []
    audit("users.imported", request, club_id=club.id, target=f"club:{club.id}",
          detail=f"créés={len(created)} modifiés={updated} ignorés={counts['skip']} erreurs={counts['error']}")
    return render(request, "admin_users_import.html", clubs=clubs, club=club, mail_ok=mail_ok, result=dict(
        created=len(created), updated=updated, skipped=counts["skip"], errors=counts["error"],
        invited=invited and bool(created)), credentials=credentials)


# --- Profil personnel --------------------------------------------------------------

@router.post("/account/profile")
def profile_update(request: Request, first_name: str = Form(""), last_name: str = Form(""), phone: str = Form(""),
                   user: User = Depends(login_required), db: Session = Depends(get_db)):
    try:
        first, last, _, new_phone = people.validate_person(first_name, last_name, user.email, phone)
    except people.ValidationError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/password", status_code=303)
    user.first_name, user.last_name, user.phone = first, last, new_phone
    db.commit()
    audit("account.profile_updated", request, target=f"user:{user.id}")
    flash(request, "Profil mis à jour.", "ok")
    return RedirectResponse("/password", status_code=303)
