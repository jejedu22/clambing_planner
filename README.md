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

## Pages publiques et demandes de création de club

- **`/`** présente l'application aux visiteurs non connectés (les membres connectés y voient leur agenda). Aucune donnée de club n'y figure.
- **`/creer-un-club`** : formulaire public de demande (nom et ville du club, responsable, email, téléphone et effectif facultatifs, message, consentement). Protégé par jeton CSRF, champ piège anti-robots et limites (3 demandes par heure et par IP, 2 par jour et par adresse) ; une seule demande en cours par adresse ; un nom de club existant est refusé. Les super administrateurs sont prévenus par email (SMTP), sans les coordonnées du demandeur.
- **Gestion des demandes** (*Club → Demandes de clubs*, super administrateur) : liste par statut, fiche détaillée avec alertes (nom de club pris, email ayant déjà un compte). **Accepter** crée le club (nom modifiable) et le compte administrateur du demandeur, qui reçoit par SMTP un lien d'activation valable 7 jours (sans SMTP : mot de passe provisoire affiché). **Refuser** avec un motif facultatif, envoyé au demandeur si souhaité. **Supprimer** efface la demande et les données du demandeur. Tout est tracé dans le journal d'audit.

## Clubs (super administrateur)

*Club → Clubs* liste les clubs. **Modifier** ouvre la fiche d'un club : renommage (nom unique, casse ignorée, tracé dans le journal d'audit), résumé (membres, administrateurs, sorties, favoris, localisation, intégrations) et accès directs à ses membres, sa localisation, ses favoris, ses intégrations, ses adhésions et son journal. Un club sans membres ni sorties peut être supprimé.

## Interface

- **Agenda** (accueil) : la semaine en tête (sorties du jour et météo du site), puis les sorties à venir avec la réponse **Je viens / Peut-être / Non** et les places de covoiturage directement depuis la liste. Colonne de droite : sorties en attente de réponse, covoiturage de la prochaine sortie et, pour les administrateurs, ce qui reste à faire pour le club.
- **PC** : onglets en haut (Agenda, Sites, Favoris, Membres, Club). **Mobile** : barre d'onglets en bas, réponses en gros boutons, tableaux affichés en fiches. La page **Club** regroupe le compte et l'administration.
- **Thème** : Automatique (suit le réglage de l'appareil), Clair ou Sombre, depuis le menu du compte, la page Club ou le bas de la page de connexion. Le choix est mémorisé sur l'appareil (cookie). La carte s'assombrit aussi en thème sombre.
- Police Manrope (licence OFL) servie par l'application : aucun appel à un service de polices externe.

## Sites favoris par club

Chaque club garde sa propre liste de sites favoris, avec une **note** facultative visible de ses membres (parking, accès, difficulté…). Les administrateurs de club les gèrent (bouton ☆/★ dans la liste des sites ou sur la fiche d'un site) ; les membres les consultent sur la page **⭐ Favoris**. Un club ne voit jamais les favoris ni les notes d'un autre club. Les favoris apparaissent avec une ⭐ dans la liste et sur la carte, se filtrent (*Favoris du club*) et sont proposés en premier quand on choisit le site d'une sortie. Le super administrateur gère les favoris de n'importe quel club depuis la fiche d'un site ou la page Favoris. Limite : 500 favoris par club.

**Localisation du club** (*Club → Localisation du club*, administrateurs) : un point choisi sur une carte (ou la position de l'appareil), avec un nom facultatif. La carte des sites s'ouvre alors cadrée sur ce point **et** sur tous les favoris du club (repère « maison » sur la carte) ; sans localisation, sur les favoris ; sans les deux, sur la France. Aucun service de géocodage externe n'est utilisé.

## Gestion des utilisateurs

- **Fiche** : prénom, nom, email (identifiant de connexion), téléphone (facultatif, normalisé au format international : `06 12 34 56 78` devient `+33612345678`), rôle, actif. Chacun modifie son prénom/nom/téléphone dans *Mon compte* ; l'email et le rôle ne se changent que par un administrateur (qui invalide alors les liens de réinitialisation en attente). Liste avec recherche (nom, email, téléphone) et pagination.
- **Export CSV** (*Utilisateurs → Exporter*) : séparateur `;` et UTF-8 avec BOM (s'ouvre directement dans Excel), colonnes `prenom;nom;email;telephone;role;club;actif;cotisation`. Un administrateur de club n'exporte que son club ; les cellules commençant par `=`, `+`, `-`, `@` sont neutralisées contre l'injection de formules. L'export est tracé dans le journal d'audit (données personnelles).
- **Import CSV** (*Utilisateurs → Importer*) en deux temps : une **simulation** (créations, modifications, inchangés, erreurs ligne par ligne) puis une confirmation, qui revalide tout côté serveur. Colonnes `prenom;nom;email` (+ `telephone`, `role` facultatives), séparateur `;` `,` ou tabulation, UTF-8 ou Excel Windows, 2 000 lignes / 500 Ko maximum. Un email déjà présent dans le club est **mis à jour** (une cellule vide n'efface rien, le rôle n'est jamais modifié) ; un email d'un autre club ou d'un super administrateur est refusé ; un import ne crée jamais de super administrateur. Les nouveaux comptes reçoivent une invitation par email (si Mailjet est configuré) ou un mot de passe provisoire affiché une seule fois. Un modèle est téléchargeable, et un fichier d'export peut être réimporté tel quel.
- **Migration automatique** : au démarrage, les colonnes ajoutées (prénom, nom, téléphone) sont créées sur une base existante, et le prénom/nom des comptes existants sont déduits de leur nom (premier mot = prénom) : à vérifier si des noms sont composés.

## Emails (Mailjet) et HelloAsso

Chaque club configure **ses propres** identifiants dans **Intégrations** (réservé à l'administrateur du club et au super administrateur). Les clés sont chiffrées en base et ne sont jamais réaffichées ; chiffrement dérivé de `SECRET_KEY` : si vous la changez, les clés enregistrées deviennent illisibles et doivent être ressaisies.

**Mailjet** : clé API + clé secrète + adresse d'expéditeur **validée chez Mailjet**. Boutons « Vérifier les clés » (mode bac à sable, rien n'est envoyé) et « M'envoyer un email de test ».
Emails envoyés via Mailjet (le mot de passe oublié n'en fait **pas** partie, voir plus bas) :
- *invitation* d'un nouvel utilisateur (lien valable 7 jours) à la place d'un mot de passe provisoire ;
- *notifications de sorties* : nouvelle sortie (tout le club), modification / annulation (inscrits « oui » ou « peut-être »), rappel la veille avec la météo. Chaque membre peut les couper dans « Mon compte » ;

Sans Mailjet configuré pour un club, ces emails ne partent pas : l'administrateur transmet un mot de passe provisoire comme avant.
Variable obligatoire pour les liens : `APP_BASE_URL` (déduite de `APP_HOST` par le compose, jamais de l'en-tête `Host`).

### Mot de passe oublié (SMTP classique)

Indépendant de Mailjet et des clubs : un seul serveur SMTP pour toute l'instance, configuré dans le `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_SECURITY` = `starttls` | `ssl` | `none`). Il fonctionne pour **tous** les comptes, super administrateur compris.
- lien valable 1 h, à usage unique (seul son hash est stocké), le précédent est invalidé à chaque nouvelle demande ;
- la réponse est identique que le compte existe ou non ; 3 demandes par heure et par adresse (15 par IP) ;
- un changement de mot de passe ferme les sessions ouvertes sur les autres appareils ;
- sans SMTP configuré, rien n'est envoyé (et c'est journalisé) : secours en ligne de commande, `docker compose exec app python -m app.cli reset-password EMAIL`.

## Journaux

- **Application** : `LOG_LEVEL` (`INFO` par défaut), `LOG_FORMAT` = `text` ou `json` (Loki, ELK…). Sortie standard (`docker compose logs -f app`), avec rotation Docker (5 × 10 Mo). Option `LOG_FILE` pour un fichier tournant (`LOG_MAX_MB`, `LOG_BACKUPS`).
- **Requêtes** : une ligne par requête (méthode, chemin, statut, durée, utilisateur, IP) avec un **identifiant de requête** (en-tête de réponse `X-Request-ID`) qui relie tous les événements d'une même requête. Sans query string, et le jeton des liens `/reset/…` est masqué ; le journal d'accès d'uvicorn, qui l'afficherait, est désactivé.
- **Audit** (page *Journal* pour les administrateurs, le super administrateur voit tout, un administrateur de club seulement son club) : connexions réussies/échouées/bloquées, changements et réinitialisations de mot de passe, création/modification d'utilisateurs, clubs, intégrations, synchronisations. Jamais de mot de passe, de clé ni d'email en clair (masqué : `a***@exemple.org`). Conservé `AUDIT_RETENTION_DAYS` jours (180 par défaut).

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
- Les tables sont créées au démarrage (`create_all`) et les colonnes ajoutées par `app/migrations.py` (ajouts de colonnes uniquement, ni suppression ni modification) : pour des évolutions plus lourdes, passez à Alembic.
- **Un seul worker** uvicorn : le rafraîchissement des sites tourne dans un thread du processus, et la limitation des tentatives de connexion est en mémoire.
- Les sites viennent d'OSM : la qualité varie (beaucoup de sites sans nom, couverture inégale). Seule la France métropolitaine est importée. Ce sont les données OSM, pas un topo officiel.
- Sources de données : © contributeurs OpenStreetMap (ODbL) ; météo [Open-Meteo](https://open-meteo.com/) (API gratuite pour usage non commercial, quota par IP).
- Les tuiles de carte viennent de `tile.openstreetmap.org` (usage modéré uniquement, voir leur politique d'utilisation).
