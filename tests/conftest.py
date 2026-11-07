import os

import pytest

from tests.support import POSTGRES


def pytest_collection_modifyitems(config, items):
    marked = [item for item in items if item.get_closest_marker("postgres")]
    on_postgres = os.environ.get("DTD_TEST_DB") == "postgres"
    if on_postgres:
        # A CI Postgres job with no Postgres tests would pass while proving nothing.
        if os.environ.get("CI") and not marked:
            raise pytest.UsageError("Postgres job collected no Postgres tests")
        return
    skip = pytest.mark.skip(reason="needs Postgres (DTD_TEST_DB=postgres)")
    for item in marked:
        item.add_marker(skip)


def _use_login(alias, user):
    from django.db import connections

    settings_dict = connections[alias].settings_dict
    settings_dict["USER"] = settings_dict["PASSWORD"] = user
    connections[alias].close()


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings_parallel_suffix):
    """Django always builds the test database through `default`, so lend it the owner's login."""
    if POSTGRES:
        _use_login("default", "dtd_migrator")


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    if not POSTGRES:
        yield
        return
    from django.db import connections

    from django_tenant_domains.rls.policies import sync_policies

    with django_db_blocker.unblock():
        _use_login("default", "dtd_app")
        # Migrate ran as `default`, so the policy step that follows a migrate skipped itself.
        sync_policies()
        # Test cleanup truncates as the application's login. Production never grants this.
        with connections["migrator"].cursor() as cursor:
            cursor.execute("GRANT TRUNCATE ON ALL TABLES IN SCHEMA public TO dtd_app")
    yield
    with django_db_blocker.unblock():
        connections["migrator"].close()
        _use_login("default", "dtd_migrator")
