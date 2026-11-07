"""Settings for migrating a database other than the default one."""

import os

from tests.settings import *

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ["DTD_DEFAULT_DB"]},
    "other": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ["DTD_OTHER_DB"]},
}


class SplitRouter:
    """Reads from default, writes to other, as a read replica setup would."""

    def db_for_read(self, model, **hints):
        return "default"

    def db_for_write(self, model, **hints):
        return "other"

    def allow_relation(self, *args, **hints):
        return True


if os.environ.get("DTD_ROUTER"):
    DATABASE_ROUTERS = [os.environ["DTD_ROUTER"]]
