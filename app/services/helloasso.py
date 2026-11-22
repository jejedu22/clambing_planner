"""Client minimal de l'API HelloAsso v5 (OAuth2 client_credentials, lecture seule).

Routes utilisées (spec : https://api.helloasso.com/v5/swagger/public/swagger.json) :
  POST /oauth2/token
  GET  /v5/organizations/{org}/forms
  GET  /v5/organizations/{org}/forms/{formType}/{formSlug}/items
Le client API doit avoir les privilèges AccessPublicData (liste des formulaires) et
AccessTransactions (articles / paiements).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..config import settings

FORM_TYPES = ("CrowdFunding", "Membership", "Event", "Donation", "PaymentForm", "Checkout", "Shop")
VALID_ITEM_STATES = ("Processed", "Registered")   # payé / enregistré manuellement : valides
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,118}$")
MAX_PAGES = 100
PAGE_SIZE = 100


class HelloAssoError(Exception):
    """Erreur lisible, affichable à l'administrateur."""


_tokens: dict[tuple[str, str], tuple[str, float]] = {}
_lock = threading.Lock()


@dataclass
class Item:
    id: str
    type: str
    state: str
    amount_cents: int
    email: str
    first_name: str
    last_name: str
    paid_at: datetime | None
    name: str

    @property
    def valid(self) -> bool:
        return self.state in VALID_ITEM_STATES


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_item(raw: dict) -> Item:
    payer, user = raw.get("payer") or {}, raw.get("user") or {}
    return Item(
        id=str(raw.get("id", "")), type=raw.get("type") or "", state=raw.get("state") or "",
        amount_cents=int(raw.get("amount") or 0),
        email=(payer.get("email") or "").strip().lower(),      # l'email n'existe que sur le payeur
        first_name=(user.get("firstName") or payer.get("firstName") or "").strip(),
        last_name=(user.get("lastName") or payer.get("lastName") or "").strip(),
        paid_at=_parse_date((raw.get("order") or {}).get("date")),
        name=raw.get("name") or "",
    )


class HelloAsso:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self.base = settings.helloasso_url

    # --- HTTP ---------------------------------------------------------------
    def _token(self, force: bool = False) -> str:
        key = (self.client_id, self.client_secret)
        with _lock:
            hit = _tokens.get(key)
            if hit and not force and hit[1] > time.time() + 30:
                return hit[0]
        try:
            r = httpx.post(f"{self.base}/oauth2/token", timeout=15, headers={"User-Agent": settings.user_agent},
                           data={"client_id": self.client_id, "client_secret": self.client_secret,
                                 "grant_type": "client_credentials"})
        except httpx.HTTPError as exc:
            raise HelloAssoError(f"HelloAsso injoignable ({type(exc).__name__}).") from exc
        if r.status_code in (400, 401):
            raise HelloAssoError("Identifiants HelloAsso refusés (client_id / client_secret).")
        if r.status_code != 200:
            raise HelloAssoError(f"HelloAsso a répondu {r.status_code} à l'authentification.")
        data = r.json()
        with _lock:
            _tokens[key] = (data["access_token"], time.time() + int(data.get("expires_in", 1800)))
        return data["access_token"]

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in (0, 1):
            token = self._token(force=attempt == 1)
            try:
                r = httpx.get(f"{self.base}/v5{path}", params=params, timeout=20,
                              headers={"Authorization": f"Bearer {token}", "User-Agent": settings.user_agent})
            except httpx.HTTPError as exc:
                raise HelloAssoError(f"HelloAsso injoignable ({type(exc).__name__}).") from exc
            if r.status_code == 401 and attempt == 0:
                continue   # jeton expiré : on en redemande un
            break
        if r.status_code == 401:
            raise HelloAssoError("Jeton HelloAsso refusé.")
        if r.status_code == 403:
            raise HelloAssoError("Droits insuffisants : le client API doit avoir les privilèges "
                                 "AccessPublicData et AccessTransactions.")
        if r.status_code == 404:
            raise HelloAssoError("Organisation ou formulaire introuvable (vérifiez les identifiants/slugs).")
        if r.status_code == 429:
            raise HelloAssoError("Trop de requêtes HelloAsso, réessayez dans un instant.")
        if r.status_code != 200:
            raise HelloAssoError(f"HelloAsso a répondu {r.status_code}.")
        return r.json()

    def _paginate(self, path: str, params: dict) -> list[dict]:
        out: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data = self._get(path, {**params, "pageIndex": page, "pageSize": PAGE_SIZE})
            out.extend(data.get("data") or [])
            if page >= int((data.get("pagination") or {}).get("totalPages") or 1):
                break
        return out

    # --- API ----------------------------------------------------------------
    def check(self, org: str) -> int:
        """Vérifie identifiants + organisation. Retourne le nombre de formulaires."""
        _check_slug(org)
        data = self._get(f"/organizations/{org}/forms", {"pageSize": 1, "pageIndex": 1,
                                                          "states": ["Public", "Private"]})
        return int((data.get("pagination") or {}).get("totalCount") or len(data.get("data") or []))

    def forms(self, org: str) -> list[dict]:
        _check_slug(org)
        rows = self._paginate(f"/organizations/{org}/forms", {"states": ["Public", "Private"]})
        return [{"type": f.get("formType"), "slug": f.get("formSlug"),
                 "title": f.get("title") or f.get("privateTitle") or f.get("formSlug")}
                for f in rows if f.get("formType") in FORM_TYPES and f.get("formSlug")]

    def items(self, org: str, form_type: str, form_slug: str) -> list[Item]:
        _check_slug(org), _check_slug(form_slug)
        if form_type not in FORM_TYPES:
            raise HelloAssoError("Type de formulaire invalide.")
        rows = self._paginate(f"/organizations/{org}/forms/{form_type}/{form_slug}/items", {})
        return [parse_item(r) for r in rows]


def _check_slug(slug: str) -> None:
    if not SLUG_RE.match(slug or ""):
        raise HelloAssoError("Identifiant (slug) HelloAsso invalide.")
