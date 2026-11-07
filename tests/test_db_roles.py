import psycopg
import pytest
from django.conf import settings
from django.db import connection

pytestmark = pytest.mark.postgres


@pytest.mark.django_db
def test_application_role_cannot_bypass_row_level_security():
    # Row-level security does not apply to these roles, so the suite would prove nothing.
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cursor.fetchone() == (False, False)


def test_migration_role_is_separate_and_bypasses_row_level_security():
    db = settings.DATABASES["migrator"]
    assert db["USER"] != settings.DATABASES["default"]["USER"]
    with psycopg.connect(
        host=db["HOST"],
        port=db["PORT"],
        user=db["USER"],
        password=db["PASSWORD"],
        dbname=db["NAME"],
    ) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row == (False, True)
