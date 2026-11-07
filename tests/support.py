"""Lets one test run both on SQLite, with the ORM filter only, and on Postgres with RLS."""

import contextlib
import os
from contextlib import contextmanager

import pytest
from django.db import DatabaseError, transaction

from django_tenant_domains.exceptions import TenantMismatch

POSTGRES = os.environ.get("DTD_TEST_DB") == "postgres"
# The login that sees every tenant's rows. On SQLite everything does.
BYPASS = "migrator" if POSTGRES else "default"
ALL_DATABASES = ["default", "migrator"] if POSTGRES else ["default"]
# Different connections cannot see uncommitted rows, so checking through the bypass login needs
# rows really committed.
leak_db = (
    pytest.mark.django_db(transaction=True, databases=ALL_DATABASES)
    if POSTGRES
    else pytest.mark.django_db
)
# The test records what the ORM filter alone does. Row-level security changes the outcome.
orm_only = pytest.mark.skipif(POSTGRES, reason="records ORM behavior without row-level security")


def peek(model):
    """Read every tenant's rows of a model, whatever the current tenant is."""
    return model.unscoped_objects.using(BYPASS)


def held(model, pk):
    """Their row, as attacking code would hold it: loaded earlier through the normal login."""
    instance = peek(model).get(pk=pk)
    instance._state.db = "default"
    return instance


@contextmanager
def no_effect_or_refused():
    """Row-level security hides their row, so the write finds nothing and silently does nothing.

    Without row-level security the ORM check has to refuse it. Callers still assert the row is
    unchanged, which is the point on both.
    """
    if POSTGRES:
        with contextlib.suppress(TenantMismatch, DatabaseError), transaction.atomic():
            yield
    else:
        with refused():
            yield


def refused():
    """Expect a write to fail, by the ORM check or by the database, in a savepoint."""

    class Refused:
        def __enter__(self):
            self.raises = pytest.raises((TenantMismatch, DatabaseError))
            self.atomic = transaction.atomic()
            self.raises.__enter__()
            self.atomic.__enter__()

        def __exit__(self, *exc):
            handled_atomic = self.atomic.__exit__(*exc)
            return self.raises.__exit__(*exc) or handled_atomic

    return Refused()
