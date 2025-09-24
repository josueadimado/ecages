from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-key")
DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() == "true"

# Host & CSRF configuration
_default_hosts = ["localhost", "127.0.0.1", "[::1]", "0.0.0.0"]

# Allow overriding via env; ensure robust parsing
_env_allowed = os.getenv("DJANGO_ALLOWED_HOSTS", "").strip()
if _env_allowed:
    ALLOWED_HOSTS = [h.strip() for h in _env_allowed.split(",") if h.strip()]
elif DEBUG:
    # In dev, be permissive to avoid CommandError; tighten in prod
    ALLOWED_HOSTS = ["*"]
else:
    # Production but no env provided: fall back to safe localhost defaults
    ALLOWED_HOSTS = _default_hosts

# Optional: CSRF trusted origins (needed when using a domain or reverse proxy)
# Example env: DJANGO_CSRF_TRUSTED_ORIGINS=https://ecages.example.com,https://api.ecages.example.com
_csrf_origins = os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").strip()
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "rest_framework",

    "apps.common",
    "apps.accounts",
    "apps.providers",
    "apps.products",
    "apps.inventory",
    "apps.sales",
    "apps.finance",
    "apps.hr",
    "apps.logistics",
    "apps.reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],   # <-- important
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ],
    },
}]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",     # Simple pour démarrer
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_USER_MODEL = "accounts.User"

LANGUAGE_CODE = "fr"
TIME_ZONE = "Africa/Lome"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"] 
STATIC_ROOT = BASE_DIR / "staticfiles"  

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# Django Admin Performance Optimizations
ADMIN_SITE_HEADER = "ECAGES Administration"
ADMIN_SITE_TITLE = "ECAGES Admin Portal"
ADMIN_INDEX_TITLE = "Bienvenue dans l'administration ECAGES"

# Admin performance settings
ADMIN_LIST_PER_PAGE = 50  # Reduce default pagination
ADMIN_SEARCH_HELP_TEXT = True  # Show search help text

# Increase limits to handle large admin POSTs (bulk selections/filters)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 50000  # default is 1000; raise to avoid TooManyFieldsSent

# Daily reporting configuration
DAILY_REPORT_EMAILS = [
    'admin@ecages.com',  # Replace with actual email addresses
    'manager@ecages.com',
]

DAILY_REPORT_WHATSAPP = [
    '+22812345678',  # Replace with actual phone numbers
    '+22887654321',
]

# Email configuration (for production, use proper SMTP settings)
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'  # For development
# EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'  # For production
# EMAIL_HOST = 'smtp.gmail.com'
# EMAIL_PORT = 587
# EMAIL_USE_TLS = True
# EMAIL_HOST_USER = 'your-email@gmail.com'
# EMAIL_HOST_PASSWORD = 'your-app-password'
DEFAULT_FROM_EMAIL = 'noreply@ecages.com'