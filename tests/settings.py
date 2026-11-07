"""Test settings. SQLite by default; Postgres when DTD_TEST_DB=postgres."""

import os

SECRET_KEY = "test-only"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django_tenant_domains",
    "django_tenant_domains.contrib.signup",
    "tests.thirdparty",
    "tests.testapp",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_tenant_domains.middleware.TenantMiddleware",
]
AUTHENTICATION_BACKENDS = ["django_tenant_domains.backends.TenantBackend"]
ROOT_URLCONF = "tests.urls"
DATABASE_ROUTERS = ["django_tenant_domains.routers.TenantAdminRouter"]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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

if os.environ.get("DTD_TEST_DB") == "postgres":
    _server = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "dtd",
        "HOST": os.environ.get("DTD_DB_HOST", "localhost"),
        "PORT": os.environ.get("DTD_DB_PORT", "55432"),
    }
    # Both logins share one test database. tests/conftest.py has the migrator build it, so it
    # owns the tables, as it would in production.
    DATABASES = {
        # The role for migrations and admin tasks, which bypasses row-level security.
        "migrator": {
            **_server,
            "USER": "dtd_migrator",
            "PASSWORD": "dtd_migrator",
        },
        # The ordinary role the application connects as. Row-level security binds it.
        "default": {**_server, "USER": "dtd_app", "PASSWORD": "dtd_app"},
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
