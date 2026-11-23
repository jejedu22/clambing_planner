"""Pages publiques (sans connexion) : présentation et demande de création de club."""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import SessionLocal, get_db
from ..logging_setup import mask_email
from ..models import Club, ClubRequest, Role, Site, User
from ..security import Throttle
from ..services import mailer, people, smtp_mailer
from ..web import render

router = APIRouter()
log = logging.getLogger("public")
by_ip = Throttle(limit=3, window=3600)          # 3 demandes / heure / IP
by_email = Throttle(limit=2, window=24 * 3600)  # 2 demandes / jour / adresse


def landing(request: Request, db: Session):
    sites = db.scalar(select(func.count()).select_from(Site).where(Site.outdoor.is_(True))) or 0
    return render(request, "landing.html", sites=sites)


def _notify_super_admins(request_id: int) -> None:
    """Prévient les super administrateurs par SMTP (tâche de fond). Ne contient aucune donnée du demandeur
    au-delà du nom du club : le détail se consulte dans l'application."""
    if not smtp_mailer.configured():
        return
    with SessionLocal() as db:
        req = db.get(ClubRequest, request_id)
        if req is None:
            return
        admins = db.scalars(select(User).where(User.role == Role.SUPER_ADMIN.value, User.is_active.is_(True))).all()
        url = mailer.link(f"/admin/requests/{request_id}")
        mails = []
        for a in admins:
            subject = "Nouvelle demande de création de club"
            text, html = mailer.build(subject, a.name, [f"Une demande a été déposée pour le club « {req.club_name} » ({req.city})."],
                                      ("Examiner la demande", url) if url else None)
            mails.append(mailer.Mail(a.email, a.name, subject, text, html))
    for m in mails:
        smtp_mailer.send(m)


@router.get("/creer-un-club")
def request_form(request: Request):
    return render(request, "club_request.html", form={})


@router.post("/creer-un-club")
def request_submit(request: Request, background: BackgroundTasks, club_name: str = Form(""), city: str = Form(""),
                   first_name: str = Form(""), last_name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                   members: str = Form(""), message: str = Form(""), consent: str = Form(""),
                   website: str = Form(""), db: Session = Depends(get_db)):
    form = dict(club_name=club_name, city=city, first_name=first_name, last_name=last_name, email=email,
                phone=phone, members=members, message=message)
    ip = request.client.host if request.client else "?"
    if website:   # champ piège invisible : rempli uniquement par les robots. On fait comme si de rien n'était.
        log.warning("Demande de club rejetée (champ piège)")
        return render(request, "club_request.html", sent=True)

    def fail(msg: str, code: int = 400):
        return render(request, "club_request.html", code, form=form, error=msg)

    try:
        name = " ".join(club_name.split())
        place = " ".join(city.split())
        if not (2 <= len(name) <= 120):
            raise people.ValidationError("Indiquez le nom du club (2 à 120 caractères).")
        if not (2 <= len(place) <= 120):
            raise people.ValidationError("Indiquez la ville ou le secteur du club.")
        first, last, mail, tel = people.validate_person(first_name, last_name, email, phone)
        if not (first and last):
            raise people.ValidationError("Indiquez votre prénom et votre nom.")
        count = int(members) if members.strip() else None
        if count is not None and not 1 <= count <= 100000:
            raise people.ValidationError("Effectif invalide.")
        note = message.strip()
        if len(note) > 2000:
            raise people.ValidationError("Message trop long (2 000 caractères maximum).")
        if consent != "1":
            raise people.ValidationError("Merci d'accepter le traitement de vos données pour étudier la demande.")
    except people.ValidationError as exc:
        return fail(str(exc))
    except ValueError:
        return fail("Effectif invalide.")

    if by_ip.blocked(ip) or by_email.blocked(mail):
        audit("club_request.throttled", request, target=mask_email(mail))
        return fail("Trop de demandes envoyées. Réessayez plus tard.", 429)
    if db.scalar(select(Club.id).where(func.lower(Club.name) == name.lower())):
        return fail("Un club porte déjà ce nom sur Climbing Planner. Contactez son administrateur ou précisez le nom.")
    if db.scalar(select(ClubRequest.id).where(ClubRequest.email == mail, ClubRequest.status == "pending")):
        return fail("Une demande est déjà en cours d'examen pour cette adresse email.")
    by_ip.add(ip)
    by_email.add(mail)
    req = ClubRequest(club_name=name, city=place, first_name=first, last_name=last, email=mail, phone=tel,
                      members=count, message=note or None)
    db.add(req)
    db.commit()
    audit("club_request.created", request, target=f"request:{req.id}", detail=name)
    background.add_task(_notify_super_admins, req.id)
    return render(request, "club_request.html", sent=True)
