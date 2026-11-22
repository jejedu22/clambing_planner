"""Commandes d'administration : docker compose exec app python -m app.cli reset-password EMAIL

Utile surtout pour le super administrateur, qui n'appartient à aucun club et ne peut donc pas
recevoir d'email Mailjet (la configuration Mailjet est propre à chaque club).
"""
import sys

from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .migrations import upgrade_schema
from .models import User
from .security import generate_password, hash_password
from .services import tokens


def reset_password(email: str) -> int:
    Base.metadata.create_all(engine)
    upgrade_schema(engine)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None:
            print("Aucun utilisateur avec cet email.", file=sys.stderr)
            return 1
        temp = generate_password()
        user.password_hash = hash_password(temp)
        user.must_change_password = True
        user.is_active = True
        tokens.bump_epoch(db, user.id)   # ferme les sessions ouvertes
        db.commit()
    print(f"Mot de passe provisoire pour {email} : {temp}\nIl devra être changé à la prochaine connexion.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "reset-password":
        return reset_password(argv[2])
    print("Usage : python -m app.cli reset-password EMAIL", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
