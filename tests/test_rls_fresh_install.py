"""A fresh project using only the documented setup, with no test-only policy install."""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from tests.support import POSTGRES

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("DTD_DB_HOST", "localhost")
PORT = os.environ.get("DTD_DB_PORT", "55432")

PROOF = """
import django; django.setup()
from django.db import connection
from django_tenant_domains.context import tenant_context
from django_tenant_domains.models import Tenant
from tests.testapp.models import Project

a = Tenant.objects.create(slug="a", name="A")
b = Tenant.objects.create(slug="b", name="B")
with tenant_context(a):
    Project.objects.create(name="a-project")
with tenant_context(b):
    Project.objects.create(name="b-project")
with connection.cursor() as cursor:
    cursor.execute("SELECT count(*) FROM testapp_project")
    assert cursor.fetchone()[0] == 0, "raw SQL with no tenant must see nothing"
    cursor.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    assert cursor.fetchone()[0] is False
with tenant_context(a), connection.cursor() as cursor:
    cursor.execute("SELECT name FROM testapp_project")
    assert cursor.fetchall() == [("a-project",)]
"""


def _admin():
    return psycopg.connect(
        host=HOST,
        port=PORT,
        user="dtd_migrator",
        password="dtd_migrator",
        dbname="postgres",
        autocommit=True,
    )


@pytest.fixture
def database():
    name = f"dtd_fresh_{uuid.uuid4().hex[:8]}"
    with _admin() as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    yield name
    with _admin() as conn:
        # FORCE may not end another login's session. A finished subprocess's session can outlive
        # it briefly, and a background Postgres process can arrive at any moment, so try again.
        deadline = time.monotonic() + 30
        while True:
            try:
                conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
                break
            except psycopg.errors.InsufficientPrivilege:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)


def _run(database, *args):
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.settings_fresh",
        "DTD_FRESH_DB": database,
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, *args], check=False, cwd=ROOT, env=env, capture_output=True, text=True
    )


@pytest.mark.skipif(not POSTGRES, reason="needs Postgres")
def test_documented_setup_enforces_row_level_security(database):
    result = _run(database, "-m", "django", "migrate", "--database=migrator", "--no-input")
    assert result.returncode == 0, result.stderr
    result = _run(database, "-m", "django", "check", "--database", "default")
    assert result.returncode == 0, result.stdout + result.stderr
    result = _run(database, "-c", PROOF)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not POSTGRES, reason="needs Postgres")
def test_check_fails_before_migrating_through_the_bypass_login(database):
    with psycopg.connect(
        host=HOST,
        port=PORT,
        user="dtd_migrator",
        password="dtd_migrator",
        dbname=database,
        autocommit=True,
    ) as conn:
        conn.execute("CREATE TABLE testapp_tag (id bigint, name text, tenant_id bigint)")
    result = _run(database, "-m", "django", "check", "--database", "default")
    assert result.returncode != 0
    assert "tenant_domains.E003" in result.stdout + result.stderr


@pytest.mark.skipif(not POSTGRES, reason="needs Postgres")
def test_migrate_command_repairs_a_missing_policy(database):
    assert _run(database, "-m", "django", "migrate", "--database=migrator").returncode == 0
    with psycopg.connect(
        host=HOST,
        port=PORT,
        user="dtd_migrator",
        password="dtd_migrator",
        dbname=database,
        autocommit=True,
    ) as conn:
        conn.execute("DROP POLICY tenant_domains_isolation ON testapp_tag")
    broken = _run(database, "-m", "django", "check", "--database", "default")
    assert "tenant_domains.E003" in broken.stdout + broken.stderr
    repaired = _run(database, "-m", "django", "migrate", "--database=migrator")
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    result = _run(database, "-m", "django", "check", "--database", "default")
    assert result.returncode == 0, result.stdout + result.stderr
