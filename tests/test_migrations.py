import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def manage(tmp_path, *args):
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.settings_two_dbs",
        "DTD_DEFAULT_DB": str(tmp_path / "default.sqlite3"),
        "DTD_OTHER_DB": str(tmp_path / "other.sqlite3"),
    }
    return subprocess.run(
        [sys.executable, "-m", "django", *args],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def role_count(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT count(*) FROM django_tenant_domains_role").fetchone()[0]


def test_built_in_roles_migration_uses_the_database_being_migrated(tmp_path):
    result = manage(tmp_path, "migrate", "--database=other", "--no-input")
    assert result.returncode == 0, result.stderr
    assert role_count(tmp_path / "other.sqlite3") == 4

    result = manage(tmp_path, "migrate", "django_tenant_domains", "0001", "--database=other")
    assert result.returncode == 0, result.stderr
    assert role_count(tmp_path / "other.sqlite3") == 0
