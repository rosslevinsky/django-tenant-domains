"""Another tenant's third-party record must stay out of reach, with row-level security on and off."""

import pytest
from django.db import DatabaseError, transaction

from django_tenant_domains.context import _State, _switch, tenant_context
from django_tenant_domains.exceptions import TenantMismatch, TenantNotSet
from django_tenant_domains.external import external_records, share
from django_tenant_domains.models import Tenant
from tests.support import BYPASS, POSTGRES, leak_db, orm_only
from tests.testapp.models import ArticleTenantLink
from tests.thirdparty.models import Article

pytestmark = leak_db


@pytest.fixture
def theirs():
    tenant = Tenant.objects.create(slug="theirs", name="Theirs")
    with tenant_context(tenant):
        return Article.objects.create(title="theirs")


@pytest.fixture
def mine():
    return Tenant.objects.create(slug="mine", name="Mine")


def test_helper_hides_their_row(mine, theirs):
    with tenant_context(mine):
        rows = external_records(Article)
        assert not rows.exists()
        assert rows.count() == 0
        assert not rows.filter(pk=theirs.pk).exists()
        assert list(rows.values_list("title", flat=True)) == []


def test_helper_refuses_without_a_tenant():
    with pytest.raises(TenantNotSet):
        external_records(Article)


def test_helper_cannot_update_or_delete_their_row(mine, theirs):
    with tenant_context(mine):
        assert external_records(Article).filter(pk=theirs.pk).update(title="x") == 0
        assert external_records(Article).filter(pk=theirs.pk).delete()[0] == 0
    assert Article._base_manager.using(BYPASS).get(pk=theirs.pk).title == "theirs"


def test_hand_built_instance_cannot_overwrite_their_row(mine, theirs):
    with tenant_context(mine), pytest.raises((TenantMismatch, DatabaseError)), transaction.atomic():
        Article(pk=theirs.pk, title="stolen").save()
    assert Article._base_manager.using(BYPASS).get(pk=theirs.pk).title == "theirs"


def test_shared_row_stays_private_from_a_third_tenant(mine, theirs):
    owner = ArticleTenantLink.unscoped_objects.using(BYPASS).get(record_id=theirs.pk).tenant
    third = Tenant.objects.create(slug="third", name="Third")
    with tenant_context(owner):
        share(theirs, mine)
    with tenant_context(mine):
        assert external_records(Article).filter(pk=theirs.pk).exists()
    with tenant_context(third):
        assert not external_records(Article).filter(pk=theirs.pk).exists()


@pytest.mark.skipif(not POSTGRES, reason="needs row-level security")
def test_third_party_manager_is_bound_on_postgres(mine, theirs):
    with tenant_context(mine):
        assert not Article.objects.exists()
        assert not Article.objects.filter(pk=theirs.pk).exists()
        assert Article.objects.count() == 0


@orm_only
def test_third_party_manager_is_not_filtered_without_row_level_security(mine, theirs):
    """The recorded gap: third-party code uses its own manager, which only the database can bind."""
    previous = _switch(_State(tenant=mine))
    try:
        assert Article.objects.filter(pk=theirs.pk).exists()
    finally:
        _switch(previous)
