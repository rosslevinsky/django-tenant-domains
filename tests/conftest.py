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


def pytest_configure(config):
    if POSTGRES:
        _flush_each_database_once()


def _flush_each_database_once():
    """Empty the shared test database once after a test that commits, not once per login.

    `migrator` is a second login to the database `default` uses. Flushing it as well truncated
    every table twice, and its post-migrate signal rebuilt every row-level security policy each
    time, though a truncate leaves policies alone. That was over half the Postgres suite's time.
    `migrator` is not a test mirror, because a mirror would also take `default`'s login.
    """
    from django.test import TransactionTestCase

    names = TransactionTestCase._databases_names.__func__

    def without_migrator(cls, include_mirrors=True):
        aliases = names(cls, include_mirrors)
        return aliases if include_mirrors else [alias for alias in aliases if alias != "migrator"]

    TransactionTestCase._databases_names = classmethod(without_migrator)


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
