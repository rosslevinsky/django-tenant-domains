"""A project following docs/rls.md, on a database of its own."""

import os

from tests.settings import *

_server = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.environ["DTD_FRESH_DB"],
    "HOST": os.environ.get("DTD_DB_HOST", "localhost"),
    "PORT": os.environ.get("DTD_DB_PORT", "55432"),
}
DATABASES = {
    "default": {**_server, "USER": "dtd_app", "PASSWORD": "dtd_app"},
    "migrator": {**_server, "USER": "dtd_migrator", "PASSWORD": "dtd_migrator"},
}
