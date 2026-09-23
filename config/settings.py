"""Django settings for the ADG personalization project.

Two apps:

* ``feed``     -- the Braze Connected Content endpoint. Read-only, kept
                  deliberately thin; a send is a burst of concurrent requests.
* ``pipeline`` -- models, admin and management commands for the nightly job.

The scoring code itself lives in ``adgvec/`` and imports nothing from Django,
so the backtest can run in a notebook and stays testable in isolation.
"""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-not-for-production")
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [h for h in os.getenv(
    "ALLOWED_HOSTS", "personalized.arkansasonline.com,localhost,127.0.0.1"
).split(",") if h]
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS if not h.startswith("127.")
]

# --- feed endpoint -------------------------------------------------------- #
FEED_TOKEN = os.getenv("FEED_TOKEN", "")
FEED_TOKEN_HEADER = "HTTP_X_FEED_TOKEN"
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "500"))
EDITION_CACHE_SECONDS = int(os.getenv("EDITION_CACHE_SECONDS", "30"))


def _database_from_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(p.path.lstrip("/")) or "postgres",
        "USER": unquote(p.username or ""),
        "PASSWORD": unquote(p.password or ""),
        "HOST": p.hostname or "",
        "PORT": str(p.port or ""),
        "CONN_MAX_AGE": int(os.getenv("CONN_MAX_AGE", "60")),
        "OPTIONS": {},
    }


DATABASES = {"default": _database_from_url(
    os.getenv("DATABASE_URL", "postgresql:///personalization"))}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "pipeline",
    "feed",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

STATIC_URL = "static/"
# collectstatic target. Django does not serve these itself when DEBUG is off,
# so in production the admin needs nginx (or whitenoise) pointing at this
# directory -- see the Static files note in the README.
STATIC_ROOT = BASE_DIR / "static"
STATICFILES_DIRS: list = []   # project-owned assets would go here, not STATIC_ROOT
USE_TZ = True
TIME_ZONE = os.getenv("ELLINGTON_TZ", "America/Chicago")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
