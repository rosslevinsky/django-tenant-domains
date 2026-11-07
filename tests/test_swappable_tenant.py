import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INSPECT = """
import django; django.setup()
from django.db import connection
from django_tenant_domains.models import Membership, Role, Tenant, get_tenant_model
assert get_tenant_model().__name__ == "CustomTenant"
assert Tenant._meta.swapped == "swapped_tenant.CustomTenant"
assert Membership._meta.get_field("tenant").related_model is get_tenant_model()
assert Role._meta.get_field("tenant").related_model is get_tenant_model()
tables = connection.introspection.table_names()
assert "swapped_tenant_customtenant" in tables
assert "django_tenant_domains_tenant" not in tables
assert Role.objects.filter(tenant=None).count() == 4
t = get_tenant_model().objects.create(slug="acme", name="Acme", billing_code="x")
assert Role.objects.create(tenant=t, name="auditor").tenant == t
"""


def run(tmp_path, *args):
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.settings_swapped",
        "DTD_SWAP_DB": str(tmp_path / "db.sqlite3"),
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, *args], check=False, cwd=ROOT, env=env, capture_output=True, text=True
    )


def test_project_can_swap_the_tenant_model(tmp_path):
    result = run(tmp_path, "-m", "django", "migrate", "--no-input")
    assert result.returncode == 0, result.stderr
    result = run(tmp_path, "-m", "django", "makemigrations", "--check", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    result = run(tmp_path, "-c", INSPECT)
    assert result.returncode == 0, result.stderr
