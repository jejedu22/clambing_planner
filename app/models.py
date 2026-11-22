from __future__ import annotations

import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, Time,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    CLUB_ADMIN = "club_admin"
    MEMBER = "member"


ROLE_LABELS = {
    Role.SUPER_ADMIN.value: "Super administrateur",
    Role.CLUB_ADMIN.value: "Administrateur du club",
    Role.MEMBER.value: "Membre",
}


class Club(Base):
    __tablename__ = "clubs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list[User]] = relationship(back_populates="club")
    outings: Mapped[list[Outing]] = relationship(back_populates="club")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default=Role.MEMBER.value)
    # Un super administrateur n'appartient à aucun club
    club_id: Mapped[int | None] = mapped_column(ForeignKey("clubs.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    club: Mapped[Club | None] = relationship(back_populates="users")

    @property
    def is_super(self) -> bool:
        return self.role == Role.SUPER_ADMIN.value

    @property
    def is_admin(self) -> bool:
        return self.role in (Role.SUPER_ADMIN.value, Role.CLUB_ADMIN.value)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


TYPE_LABELS = {
    "sport": "Couenne",
    "boulder": "Bloc",
    "multipitch": "Grande voie",
    "trad": "Terrain d'aventure",
    "toprope": "Moulinette",
}


class Site(Base):
    """Site d'escalade importé depuis OpenStreetMap."""

    __tablename__ = "sites"
    __table_args__ = (Index("ix_sites_lat_lon", "lat", "lon"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    osm_id: Mapped[str] = mapped_column(String(30), unique=True)  # ex. "way/12345"
    name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    outdoor: Mapped[bool] = mapped_column(Boolean, default=True)
    types: Mapped[str] = mapped_column(String(100), default="")  # codes séparés par des virgules
    routes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rock: Mapped[str | None] = mapped_column(String(60), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def display_name(self) -> str:
        return self.name or "Site sans nom"

    @property
    def type_labels(self) -> list[str]:
        return [TYPE_LABELS[t] for t in self.types.split(",") if t in TYPE_LABELS]


class SiteImport(Base):
    __tablename__ = "site_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="running")  # running | ok | error
    count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Outing(Base):
    __tablename__ = "outings"

    id: Mapped[int] = mapped_column(primary_key=True)
    club_id: Mapped[int] = mapped_column(ForeignKey("clubs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    title: Mapped[str] = mapped_column(String(160))
    day: Mapped[date] = mapped_column(Date, index=True)
    meeting_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    meeting_point: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_participants: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    club: Mapped[Club] = relationship(back_populates="outings")
    site: Mapped[Site] = relationship()
    participations: Mapped[list[Participation]] = relationship(
        back_populates="outing", cascade="all, delete-orphan"
    )


class Participation(Base):
    __tablename__ = "participations"
    __table_args__ = (UniqueConstraint("outing_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    outing_id: Mapped[int] = mapped_column(ForeignKey("outings.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(10))  # yes | maybe | no
    seats: Mapped[int] = mapped_column(Integer, default=0)  # places de covoiturage proposées
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    outing: Mapped[Outing] = relationship(back_populates="participations")
    user: Mapped[User] = relationship()


# --- Emails, jetons, intégrations (tables ajoutées : create_all les crée sur une base existante) ---

class ClubIntegration(Base):
    """Réglages d'un club : Mailjet (envoi d'emails) et HelloAsso (adhésions, paiements)."""

    __tablename__ = "club_integrations"

    club_id: Mapped[int] = mapped_column(ForeignKey("clubs.id"), primary_key=True)
    mj_api_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mj_api_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # chiffré
    mj_from_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mj_from_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ha_client_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ha_client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # chiffré
    ha_org_slug: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ha_membership_form: Mapped[str | None] = mapped_column(String(120), nullable=True)
    membership_mode: Mapped[str] = mapped_column(String(10), default="season")  # season | rolling
    ha_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def mailjet_ready(self) -> bool:
        return bool(self.mj_api_key and self.mj_api_secret_enc and self.mj_from_email)

    @property
    def helloasso_ready(self) -> bool:
        return bool(self.ha_client_id and self.ha_client_secret_enc and self.ha_org_slug)


class UserPref(Base):
    __tablename__ = "user_prefs"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    # Incrémenté à chaque changement de mot de passe : invalide les autres sessions
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)


class AuthToken(Base):
    """Jeton à usage unique (mot de passe oublié, invitation). Seul le hash est stocké."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(10))  # reset | invite
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Membership(Base):
    """Adhésion importée depuis HelloAsso."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("club_id", "ha_item_id"), Index("ix_memberships_email", "club_id", "email"))

    id: Mapped[int] = mapped_column(primary_key=True)
    club_id: Mapped[int] = mapped_column(ForeignKey("clubs.id"))
    ha_item_id: Mapped[str] = mapped_column(String(30))
    email: Mapped[str] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(20))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email


class OutingForm(Base):
    """Formulaire HelloAsso lié à une sortie (paiement)."""

    __tablename__ = "outing_forms"

    outing_id: Mapped[int] = mapped_column(ForeignKey("outings.id"), primary_key=True)
    form_type: Mapped[str] = mapped_column(String(20))
    form_slug: Mapped[str] = mapped_column(String(120))
    form_title: Mapped[str | None] = mapped_column(String(255), nullable=True)


class OutingReminder(Base):
    __tablename__ = "outing_reminders"

    outing_id: Mapped[int] = mapped_column(ForeignKey("outings.id"), primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
