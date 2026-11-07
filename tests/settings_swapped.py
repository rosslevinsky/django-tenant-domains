"""Settings for a project that swaps in its own Tenant model."""

import os

from tests.settings import *

INSTALLED_APPS = [*INSTALLED_APPS, "tests.swapped_tenant"]
TENANT_DOMAINS_TENANT_MODEL = "swapped_tenant.CustomTenant"
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ["DTD_SWAP_DB"]},
}
