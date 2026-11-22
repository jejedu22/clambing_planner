# Climbing Planner

Outil de planification de sorties d'escalade pour les clubs.

- **Sites d'escalade de France** importés automatiquement depuis OpenStreetMap (Overpass API), avec carte, recherche et filtres (couenne, bloc, grande voie…).
- **Météo sur les sites** (Open-Meteo, sans clé API) : 16 jours de prévisions + 3 jours passés (pour savoir si le rocher est sec), avec un avis « bonnes conditions / risque / déconseillé » (pluie, vent, chaleur, froid).
- **Sorties par club** : création, participation (oui / peut-être / non), places limitées, covoiturage, annulation.
- **Rôles** :

| Rôle | Droits |
|---|---|
| Super administrateur | Accès à tout : clubs, utilisateurs de tous les clubs, sorties de tous les clubs, import des sites |
| Administrateur du club | Gère les utilisateurs et les sorties de **son** club |
| Membre | Voit les sorties de son club, répond, consulte sites et météo |

Un utilisateur appartient à un seul club ; les données d'un club sont invisibles pour les autres.

## Déploiement (Docker + Traefik)

Prérequis : le Traefik du dépôt [traefik](https://github.com/jejedu22/traefik) tourne et le réseau externe `proxy` existe.

```bash
cp .env.example .env     # renseigner APP_HOST, POSTGRES_PASSWORD, SECRET_KEY, SUPERADMIN_*
docker compose up -d --build
```

L'application est servie sur `https://$APP_HOST` (routeur `climbing`, entrypoint `websecure`, certificat Let's Encrypt, middleware `default@file`). La base PostgreSQL est sur un réseau interne, jamais exposée.

1. Connectez-vous avec `SUPERADMIN_EMAIL` / `SUPERADMIN_PASSWORD` (créé au premier démarrage si aucun super administrateur n'existe), puis retirez ces variables du `.env`.
2. **Import** → *Lancer un import maintenant* pour charger les sites (quelques minutes). Il est ensuite relancé tout seul tous les `SITES_REFRESH_DAYS` jours (30 par défaut, 0 pour désactiver).
3. **Clubs** → créer un club, puis **Utilisateurs** → créer son administrateur.
4. L'administrateur du club crée ses membres. Il n'y a pas d'envoi d'email : un mot de passe provisoire est affiché une seule fois à transmettre à la personne, qui doit le changer à sa première connexion.

Sauvegarde : volume Docker `pgdata` (`docker compose exec db pg_dump -U climbing climbing > backup.sql`).

## Emails (Mailjet) et HelloAsso

Chaque club configure **ses propres** identifiants dans **Intégrations** (réservé à l'administrateur du club et au super administrateur). Les clés sont chiffrées en base et ne sont jamais réaffichées ; chiffrement dérivé de `SECRET_KEY` : si vous la changez, les clés enregistrées deviennent illisibles et doivent être ressaisies.

**Mailjet** : clé API + clé secrète + adresse d'expéditeur **validée chez Mailjet**. Boutons « Vérifier les clés » (mode bac à sable, rien n'est envoyé) et « M'envoyer un email de test ».
Emails envoyés :
- *mot de passe oublié* (lien valable 1 h, à usage unique, même réponse que le compte existe ou non, limité à 3 demandes/heure par adresse) ;
- *invitation* d'un nouvel utilisateur (lien valable 7 jours) à la place d'un mot de passe provisoire ;
- *notifications de sorties* : nouvelle sortie (tout le club), modification / annulation (inscrits « oui » ou « peut-être »), rappel la veille avec la météo. Chaque membre peut les couper dans « Mon compte » ;
- les changements de mot de passe ferment les sessions ouvertes sur les autres appareils.

Sans Mailjet configuré pour un club, rien n'est envoyé : l'administrateur transmet un mot de passe provisoire comme avant. Le **super administrateur** n'a pas de club donc pas d'email : `docker compose exec app python -m app.cli reset-password EMAIL`.
Variable obligatoire pour les liens : `APP_BASE_URL` (déduite de `APP_HOST` par le compose, jamais de l'en-tête `Host`).

**HelloAsso** : identifiants API de l'association (client ID / secret, lecture seule) + slug de l'organisation. Le client doit avoir les privilèges `AccessPublicData` et `AccessTransactions`.
- **Adhésions → membres** (page *Adhésions*) : synchronisation du formulaire d'adhésion, statut « à jour » (saison sportive depuis le 1er septembre, ou 12 mois glissants), création des comptes et invitation par email. La cotisation apparaît aussi dans *Utilisateurs*.
- **Paiement des sorties** : sur une sortie, *Paiements HelloAsso* lie un formulaire (billetterie, événement, boutique…) et montre qui a payé parmi les inscrits.
- L'email vient du **payeur** HelloAsso (un parent peut payer pour son enfant) : le rapprochement se fait par email.

## Application installable (PWA)

L'appli s'installe sur l'écran d'accueil : Chrome/Edge/Android proposent « Installer », sur iPhone via *Partager → Sur l'écran d'accueil*. Elle exige HTTPS, ce que Traefik fournit.

- Les fichiers statiques (CSS, JS, icônes, carte) sont mis en cache pour un affichage rapide ; le cache se renouvelle seul à chaque déploiement.
- **Les pages et données ne sont jamais mises en cache** (sorties, participants et météo restent privés, même sur un téléphone partagé) : sans réseau, une page « Hors connexion » s'affiche.
- Fichiers : `/manifest.webmanifest`, `/sw.js` (servis à la racine, côté `app/routes/pwa.py`), icônes dans `app/static/icons/`.

## Développement

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
APP_ENV=dev uvicorn app.main:app --reload     # SQLite locale (dev.db)
```

## Choix et limites

- FastAPI + SQLAlchemy + pages HTML Jinja2 (pas de build front). Leaflet est embarqué dans `app/static/vendor`.
- Les tables sont créées au démarrage (`create_all`), sans outil de migration : à ajouter (Alembic) avant de faire évoluer le schéma en production.
- **Un seul worker** uvicorn : le rafraîchissement des sites tourne dans un thread du processus, et la limitation des tentatives de connexion est en mémoire.
- Les sites viennent d'OSM : la qualité varie (beaucoup de sites sans nom, couverture inégale). Seule la France métropolitaine est importée. Ce sont les données OSM, pas un topo officiel.
- Sources de données : © contributeurs OpenStreetMap (ODbL) ; météo [Open-Meteo](https://open-meteo.com/) (API gratuite pour usage non commercial, quota par IP).
- Les tuiles de carte viennent de `tile.openstreetmap.org` (usage modéré uniquement, voir leur politique d'utilisation).
