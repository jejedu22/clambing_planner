import os


class Settings:
    def __init__(self) -> None:
        self.env = os.environ.get("APP_ENV", "production")
        self.database_url = os.environ.get("DATABASE_URL", "sqlite:///./dev.db")
        secret = os.environ.get("SECRET_KEY", "")
        if not secret:
            if self.env != "dev":
                raise RuntimeError("SECRET_KEY est obligatoire (APP_ENV != dev)")
            secret = "dev-insecure-secret"
        self.secret_key = secret
        # Cookie "Secure" : l'application est servie en HTTPS par Traefik
        self.cookie_secure = os.environ.get("COOKIE_SECURE", "1" if self.env != "dev" else "0") == "1"
        self.superadmin_email = os.environ.get("SUPERADMIN_EMAIL", "").strip().lower()
        self.superadmin_password = os.environ.get("SUPERADMIN_PASSWORD", "")
        self.superadmin_name = os.environ.get("SUPERADMIN_NAME", "Super administrateur")
        # Import automatique des sites : 0 = désactivé
        self.sites_refresh_days = int(os.environ.get("SITES_REFRESH_DAYS", "30"))
        self.overpass_urls = [
            u.strip()
            for u in os.environ.get(
                "OVERPASS_URLS",
                "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter",
            ).split(",")
            if u.strip()
        ]
        self.weather_url = os.environ.get("WEATHER_URL", "https://api.open-meteo.com/v1/forecast")
        self.timezone = os.environ.get("TZ", "Europe/Paris")
        self.user_agent = "climbing-planner/1.0"
        # Envoi d'emails classique (SMTP) : réservé au mot de passe oublié
        self.smtp_host = os.environ.get("SMTP_HOST", "").strip()
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER", "")
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "")
        self.smtp_from = os.environ.get("SMTP_FROM", "").strip()
        self.smtp_from_name = os.environ.get("SMTP_FROM_NAME", "Climbing Planner")
        self.smtp_security = os.environ.get("SMTP_SECURITY", "starttls").lower()  # starttls | ssl | none
        # Journalisation
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.log_format = os.environ.get("LOG_FORMAT", "text").lower()          # text | json
        self.log_file = os.environ.get("LOG_FILE", "").strip()                   # vide = sortie standard seule
        self.log_max_mb = int(os.environ.get("LOG_MAX_MB", "10"))
        self.log_backups = int(os.environ.get("LOG_BACKUPS", "5"))
        self.audit_retention_days = int(os.environ.get("AUDIT_RETENTION_DAYS", "180"))
        # URL publique de l'application, utilisée dans les liens des emails.
        # Volontairement jamais déduite de l'en-tête Host (risque d'empoisonnement des liens).
        self.app_base_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
        self.mailjet_url = os.environ.get("MAILJET_API_URL", "https://api.mailjet.com")
        self.helloasso_url = os.environ.get("HELLOASSO_API_URL", "https://api.helloasso.com")


settings = Settings()
