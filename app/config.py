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


settings = Settings()
