"""Mise à niveau légère du schéma au démarrage (sans Alembic).

`create_all` crée les tables manquantes mais n'ajoute pas de colonnes à une table existante.
Chaque entrée de COLUMNS est ajoutée si elle manque (ALTER TABLE ADD COLUMN, portable
SQLite/PostgreSQL) ; les données existantes sont ensuite complétées. Idempotent.
"""
import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import User, split_name

log = logging.getLogger("migrations")

# (table, colonne, définition SQL)
COLUMNS = [
    ("users", "first_name", "VARCHAR(60)"),
    ("users", "last_name", "VARCHAR(60)"),
    ("users", "phone", "VARCHAR(32)"),
]


def upgrade_schema(engine: Engine) -> None:
    insp = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            if table in insp.get_table_names() and column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
                added.append(f"{table}.{column}")
    if added:
        log.warning("Schéma mis à niveau : colonnes ajoutées %s", ", ".join(added))
    backfill_names(engine)


def backfill_names(engine: Engine) -> int:
    """Renseigne prénom/nom des comptes existants à partir du nom d'affichage."""
    with Session(engine) as db:
        users = db.scalars(select(User).where(User.first_name.is_(None), User.last_name.is_(None))).all()
        for u in users:
            u.first_name, u.last_name = split_name(u.name)
        db.commit()
        if users:
            log.warning("Prénom/nom déduits du nom d'affichage pour %d compte(s)", len(users))
        return len(users)
