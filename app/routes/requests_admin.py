"""Gestion des demandes de création de club (super administrateur)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..models import Club, ClubRequest, Role, User, utcnow
from ..security import generate_password, hash_password, super_required
from ..services import mailer, smtp_mailer, tokens
from ..web import flash, render

router = APIRouter(prefix="/admin/requests")
STATUS_LABELS = {"pending": "En attente", "approved": "Acceptée", "rejected": "Refusée"}


def pending_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ClubRequest).where(ClubRequest.status == "pending")) or 0


def _get(db: Session, request_id: int) -> ClubRequest:
    req = db.get(ClubRequest, request_id)
    if req is None:
        raise HTTPException(404)
    return req


def _conflicts(db: Session, req: ClubRequest, club_name: str | None = None) -> list[str]:
    issues = []
    name = club_name or req.club_name
    if db.scalar(select(Club.id).where(func.lower(Club.name) == name.lower())):
        issues.append(f"Un club « {name} » existe déjà : modifiez le nom avant d'accepter.")
    existing = db.scalar(select(User).where(User.email == req.email))
    if existing:
        issues.append("Cette adresse email a déjà un compte"
                      + (f" (club {existing.club.name})" if existing.club else "") + " : impossible de créer l'administrateur.")
    return issues


@router.get("")
def requests_list(request: Request, status: str = "pending", user: User = Depends(super_required),
                  db: Session = Depends(get_db)):
    status = status if status in (*STATUS_LABELS, "all") else "pending"
    q = select(ClubRequest).order_by(ClubRequest.created_at.desc())
    if status != "all":
        q = q.where(ClubRequest.status == status)
    counts = dict(db.execute(select(ClubRequest.status, func.count()).group_by(ClubRequest.status)).all())
    return render(request, "admin_requests.html", rows=db.scalars(q.limit(200)).all(), status=status,
                  counts=counts, labels=STATUS_LABELS)


@router.get("/{request_id}")
def request_detail(request_id: int, request: Request, user: User = Depends(super_required), db: Session = Depends(get_db)):
    req = _get(db, request_id)
    return render(request, "admin_request_detail.html", req=req, labels=STATUS_LABELS,
                  conflicts=_conflicts(db, req) if req.status == "pending" else [],
                  smtp_ok=smtp_mailer.configured() and bool(mailer.link("/")),
                  club=db.get(Club, req.club_id) if req.club_id else None)


@router.post("/{request_id}/approve")
def request_approve(request_id: int, request: Request, club_name: str = Form(""), user: User = Depends(super_required),
                    db: Session = Depends(get_db)):
    req = _get(db, request_id)
    if req.status != "pending":
        flash(request, "Cette demande a déjà été traitée.", "error")
        return RedirectResponse(f"/admin/requests/{req.id}", status_code=303)
    name = " ".join(club_name.split()) or req.club_name
    if not 2 <= len(name) <= 120:
        flash(request, "Nom de club invalide.", "error")
        return RedirectResponse(f"/admin/requests/{req.id}", status_code=303)
    issues = _conflicts(db, req, name)
    if issues:
        for msg in issues:
            flash(request, msg, "error")
        return RedirectResponse(f"/admin/requests/{req.id}", status_code=303)

    club = Club(name=name)
    db.add(club)
    db.flush()
    temp = generate_password()
    admin = User(email=req.email, first_name=req.first_name, last_name=req.last_name, phone=req.phone,
                 name=req.contact_name, role=Role.CLUB_ADMIN.value, club_id=club.id,
                 password_hash=hash_password(temp), must_change_password=True)
    db.add(admin)
    req.status, req.decided_at, req.decided_by, req.club_id = "approved", utcnow(), user.id, club.id
    if name != req.club_name:
        req.decision_note = f"Club créé sous le nom « {name} »"
    db.commit()
    audit("club_request.approved", request, club_id=club.id, target=f"request:{req.id}", detail=name)
    audit("club.created", request, club_id=club.id, target=f"club:{club.id}", detail=name)

    # Invitation par SMTP (le nouveau club n'a pas encore de Mailjet) ; sinon mot de passe provisoire à transmettre
    sent = False
    base = mailer.link("/reset/")
    if smtp_mailer.configured() and base:
        admin.must_change_password = False
        raw = tokens.create_token(db, admin, "invite")
        subject = f"Votre club « {name} » est créé sur Climbing Planner"
        text, html = mailer.build(subject, admin.name, [
            f"Votre demande a été acceptée : le club « {name} » est créé et vous en êtes l'administrateur.",
            "Choisissez votre mot de passe grâce au lien ci-dessous (valable 7 jours), puis invitez vos membres."],
            ("Activer mon compte", base + raw))
        sent, err = smtp_mailer.send(mailer.Mail(admin.email, admin.name, subject, text, html))
        if not sent:
            admin.must_change_password = True
            db.commit()
            audit("user.invite_failed", request, club_id=club.id, target=f"user:{admin.id}", detail=err)
    return render(request, "admin_request_detail.html", req=req, labels=STATUS_LABELS, conflicts=[], club=club,
                  smtp_ok=smtp_mailer.configured(), approved=True, invited=sent,
                  credentials=None if sent else {"email": admin.email, "password": temp})


@router.post("/{request_id}/reject")
def request_reject(request_id: int, request: Request, note: str = Form(""), notify: str = Form(""),
                   user: User = Depends(super_required), db: Session = Depends(get_db)):
    req = _get(db, request_id)
    if req.status != "pending":
        flash(request, "Cette demande a déjà été traitée.", "error")
        return RedirectResponse(f"/admin/requests/{req.id}", status_code=303)
    note = " ".join(note.split())[:500]
    req.status, req.decided_at, req.decided_by, req.decision_note = "rejected", utcnow(), user.id, note or None
    db.commit()
    audit("club_request.rejected", request, target=f"request:{req.id}", detail=req.club_name)
    if notify == "1":
        subject = f"Votre demande de club « {req.club_name} »"
        paragraphs = [f"Votre demande de création du club « {req.club_name} » sur Climbing Planner n'a pas été retenue."]
        if note:
            paragraphs.append(f"Motif : {note}")
        text, html = mailer.build(subject, req.contact_name, paragraphs)
        ok, _ = smtp_mailer.send(mailer.Mail(req.email, req.contact_name, subject, text, html))
        flash(request, "Demande refusée, le demandeur a été prévenu." if ok else
              "Demande refusée, mais l'email n'a pas pu être envoyé.", "ok" if ok else "error")
    else:
        flash(request, "Demande refusée.", "ok")
    return RedirectResponse("/admin/requests", status_code=303)


@router.post("/{request_id}/delete")
def request_delete(request_id: int, request: Request, user: User = Depends(super_required), db: Session = Depends(get_db)):
    req = _get(db, request_id)
    db.delete(req)
    db.commit()
    audit("club_request.deleted", request, target=f"request:{request_id}")
    flash(request, "Demande supprimée (données du demandeur effacées).", "ok")
    return RedirectResponse("/admin/requests", status_code=303)
