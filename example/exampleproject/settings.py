"""Small project showing the package in use. Not for production."""

from pathlib import Path

import django

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "example-only"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_URLCONF = "exampleproject.urls"
STATIC_URL = "static/"
LOGIN_REDIRECT_URL = "/tenants/"
# Prints activation emails in the terminal running the server. Django 6.1 replaced EMAIL_BACKEND
# with MAILERS, and refuses to start with both.
CONSOLE_EMAIL = "django.core.mail.backends.console.EmailBackend"
if django.VERSION >= (6, 1):
    MAILERS = {"default": {"BACKEND": CONSOLE_EMAIL}}
else:
    EMAIL_BACKEND = CONSOLE_EMAIL

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django_tenant_domains",
    "django_tenant_domains.contrib.signup",
    "address_book",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_tenant_domains.middleware.TenantMiddleware",
]

AUTHENTICATION_BACKENDS = ["django_tenant_domains.backends.TenantBackend"]
DATABASE_ROUTERS = ["django_tenant_domains.routers.TenantAdminRouter"]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django_tenant_domains.context_processors.tenant",
            ],
        },
    },
]

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"},
}
