"""Gestion des utilisateurs : validation (noms, téléphone) et import/export CSV."""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Club, ROLE_LABELS, Role, User
from ..security import generate_password, hash_password
from .mailer import EMAIL_RE

MAX_NAME = 55            # prénom ou nom (le nom d'affichage tient dans 120 caractères)
MAX_ROWS = 2000
MAX_BYTES = 500_000
PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
IMPORT_ROLES = (Role.MEMBER.value, Role.CLUB_ADMIN.value)   # un import ne crée jamais de super administrateur


class ValidationError(ValueError):
    """Message lisible, affichable tel quel à l'administrateur."""


# --- Champs ---------------------------------------------------------------

def clean_name(raw: str | None, label: str) -> str:
    value = " ".join((raw or "").split())
    if len(value) > MAX_NAME:
        raise ValidationError(f"{label} trop long ({MAX_NAME} caractères maximum).")
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise ValidationError(f"{label} contient des caractères invalides.")
    return value


def clean_email(raw: str | None) -> str:
    email = (raw or "").strip().lower()
    if len(email) > 255 or not EMAIL_RE.match(email):
        raise ValidationError("Adresse email invalide.")
    return email


def normalize_phone(raw: str | None) -> str | None:
    """Numéro au format international (+33612345678). Les numéros français en 0X… sont convertis."""
    value = (raw or "").strip()
    if not value:
        return None
    digits = re.sub(r"[ .\-()/]", "", value)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if re.fullmatch(r"0[1-9][0-9]{8}", digits):
        digits = "+33" + digits[1:]
    if not PHONE_RE.match(digits):
        raise ValidationError("Numéro de téléphone invalide.")
    return digits


def format_phone(phone: str | None) -> str:
    """Affichage : 06 12 34 56 78 pour les numéros français, tel quel sinon."""
    if phone and phone.startswith("+33") and len(phone) == 12:
        n = "0" + phone[3:]
        return " ".join(n[i:i + 2] for i in range(0, 10, 2))
    return phone or ""


def validate_person(first: str | None, last: str | None, email: str | None, phone: str | None):
    first, last = clean_name(first, "Prénom"), clean_name(last, "Nom")
    if not (first or last):
        raise ValidationError("Indiquez au moins un prénom ou un nom.")
    return first, last, clean_email(email), normalize_phone(phone)


# --- CSV ------------------------------------------------------------------

EXPORT_COLUMNS = ["prenom", "nom", "email", "telephone", "role", "club", "actif", "cotisation"]
_SAFE_NUMBER = re.compile(r"^\+?[0-9]+$")
_HEADERS = {
    "prenom": "first_name", "firstname": "first_name", "nom": "last_name", "lastname": "last_name",
    "email": "email", "mail": "email", "courriel": "email", "emails": "email",
    "telephone": "phone", "tel": "phone", "phone": "phone", "portable": "phone", "mobile": "phone",
    "role": "role",
}


def safe_cell(value) -> str:
    """Neutralise l'injection de formules (=, +, -, @) à l'ouverture dans Excel / LibreOffice."""
    v = "" if value is None else str(value)
    if v[:1] in ("=", "+", "-", "@", "\t", "\r") and not _SAFE_NUMBER.match(v):
        return "'" + v
    return v


def _key(header: str) -> str:
    h = unicodedata.normalize("NFKD", header.strip().lower())
    return re.sub(r"[^a-z]", "", "".join(c for c in h if not unicodedata.combining(c)))


def decode(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ValidationError(f"Fichier trop volumineux ({MAX_BYTES // 1000} Ko maximum).")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")   # export Excel français


def parse_csv(text: str) -> list[dict]:
    """Lit le CSV (séparateur ; , ou tabulation détecté). Retourne des lignes {first_name, last_name, email, phone, role, line}."""
    lines = text.splitlines()
    if not lines:
        raise ValidationError("Le fichier est vide.")
    head = lines[0]
    delim = max((";", ",", "\t"), key=head.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        raise ValidationError("Le fichier est vide ou illisible.")
    cols = {i: _HEADERS[k] for i, h in enumerate(header) if (k := _key(h)) in _HEADERS}
    mapped = set(cols.values())
    if "email" not in mapped or not ({"first_name", "last_name"} & mapped):
        raise ValidationError("Colonnes attendues : prenom, nom, email (obligatoires), telephone, role (facultatives). "
                              f"Colonnes trouvées : {', '.join(h.strip() for h in header if h.strip()) or 'aucune'}.")
    rows = []
    try:
        for n, rec in enumerate(reader, start=2):
            if not any(c.strip() for c in rec):
                continue
            if len(rows) >= MAX_ROWS:
                raise ValidationError(f"Trop de lignes ({MAX_ROWS} maximum par import).")
            row = {"line": n, "first_name": "", "last_name": "", "email": "", "phone": "", "role": ""}
            for i, field_name in cols.items():
                if i < len(rec):
                    row[field_name] = rec[i].strip()
            rows.append(row)
    except csv.Error as exc:
        raise ValidationError(f"CSV illisible : {exc}")
    if not rows:
        raise ValidationError("Aucune ligne de données.")
    return rows


def export_csv(users: list[User], club_names: dict[int, str], dues: dict[str, bool]) -> bytes:
    """CSV pour Excel français : séparateur « ; », UTF-8 avec BOM."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(EXPORT_COLUMNS)
    for u in sorted(users, key=lambda x: ((x.last_name or x.name).lower(), (x.first_name or "").lower())):
        w.writerow([safe_cell(v) for v in (
            u.first_name or "", u.last_name or "", u.email, u.phone or "", u.role,
            club_names.get(u.club_id, ""), "oui" if u.is_active else "non",
            "" if u.email not in dues else ("a_jour" if dues[u.email] else "expiree"))])
    return ("﻿" + out.getvalue()).encode("utf-8")


TEMPLATE_CSV = ("﻿prenom;nom;email;telephone;role\r\n"
                "Camille;Martin;camille.martin@exemple.org;06 12 34 56 78;member\r\n"
                "Alex;Durand;alex.durand@exemple.org;;club_admin\r\n").encode("utf-8")


# --- Import ---------------------------------------------------------------

@dataclass
class PlanRow:
    line: int
    action: str            # create | update | skip | error
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    phone: str | None = None
    role: str = Role.MEMBER.value
    message: str = ""
    user_id: int | None = None
    changes: list[str] = field(default_factory=list)


def build_plan(db: Session, club: Club, rows: list[dict]) -> list[PlanRow]:
    """Simulation : ce qui serait créé / modifié / ignoré. Aucune écriture."""
    plan: list[PlanRow] = []
    seen: set[str] = set()
    for r in rows:
        row = PlanRow(line=r["line"], action="error", email=(r["email"] or "").strip().lower())
        try:
            first, last, email, phone = validate_person(r["first_name"], r["last_name"], r["email"], r["phone"])
            row.email, row.first_name, row.last_name, row.phone = email, first, last, phone
            role = (r["role"] or "").strip().lower() or Role.MEMBER.value
            if role not in IMPORT_ROLES:
                allowed = ", ".join(IMPORT_ROLES)
                raise ValidationError(f"Rôle « {r['role']} » invalide (valeurs : {allowed}).")
            row.role = role
            if email in seen:
                raise ValidationError("Email en double dans le fichier.")
            seen.add(email)
            existing = db.scalar(select(User).where(User.email == email))
            if existing is None:
                row.action = "create"
            elif existing.club_id != club.id or existing.is_super:
                raise ValidationError("Cet email appartient à un compte hors de ce club.")
            else:
                row.user_id = existing.id
                # Mise à jour prudente : un champ vide dans le fichier n'efface rien, le rôle n'est jamais modifié.
                for label, old, new in (("prénom", existing.first_name or "", first), ("nom", existing.last_name or "", last),
                                        ("téléphone", existing.phone or "", phone or "")):
                    if new and new != old:
                        row.changes.append(label)
                row.action = "update" if row.changes else "skip"
                row.message = "inchangé" if not row.changes else "modifie : " + ", ".join(row.changes)
        except ValidationError as exc:
            row.action, row.message = "error", str(exc)
        plan.append(row)
    return plan


def apply_plan(db: Session, club: Club, plan: list[PlanRow]) -> tuple[list[User], int, list[dict]]:
    """Applique la simulation. Retourne (comptes créés, nb de mises à jour, mots de passe provisoires)."""
    created: list[User] = []
    updated = 0
    credentials: list[dict] = []
    for row in plan:
        if row.action == "create":
            temp = generate_password()
            user = User(email=row.email, first_name=row.first_name, last_name=row.last_name, phone=row.phone,
                        name=f"{row.first_name} {row.last_name}".strip(), role=row.role, club_id=club.id,
                        password_hash=hash_password(temp), must_change_password=True)
            db.add(user)
            created.append(user)
            credentials.append({"email": row.email, "name": user.name, "password": temp})
        elif row.action == "update":
            user = db.get(User, row.user_id)
            if row.first_name:
                user.first_name = row.first_name
            if row.last_name:
                user.last_name = row.last_name
            if row.phone:
                user.phone = row.phone
            updated += 1
    db.commit()
    return created, updated, credentials
