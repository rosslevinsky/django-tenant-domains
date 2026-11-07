"""The smallest forms of cases tests/leak/test_fuzz.py checks, kept as fixed regression tests."""

import pytest
from django.db import transaction

from django_tenant_domains.context import tenant_context
from django_tenant_domains.exceptions import TenantMismatch
from django_tenant_domains.models import Tenant
from django_tenant_domains.sharing import READ, share
from tests.support import ALL_DATABASES, BYPASS
from tests.testapp.models import Album

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.django_db(transaction=True, databases=ALL_DATABASES),
]


@pytest.fixture
def read_only_album():
    acme = Tenant.objects.create(slug="acme", name="Acme")
    globex = Tenant.objects.create(slug="globex", name="Globex")
    with tenant_context(acme):
        album = Album.objects.create(name="shared")
        share(album, globex, READ)
    return album, globex


def test_select_for_update_leaves_out_records_shared_read_only(read_only_album):
    """Postgres applies a table's UPDATE policy to SELECT ... FOR UPDATE. See docs/known-gaps.md."""
    album, globex = read_only_album
    with tenant_context(globex):
        assert list(Album.objects.all()) == [album]
        assert list(Album.objects.select_for_update()) == []


def test_update_or_create_on_a_read_only_record_is_refused(read_only_album):
    """As on SQLite, rather than creating a second record the lock could not see."""
    _, globex = read_only_album
    with tenant_context(globex), pytest.raises(TenantMismatch), transaction.atomic():
        Album.objects.update_or_create(name="shared", defaults={"name": "changed"})
    assert not Album.unscoped_objects.using(BYPASS).filter(name="changed").exists()
    assert Album.unscoped_objects.using(BYPASS).filter(name="shared").count() == 1
