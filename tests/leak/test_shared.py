"""The same attacks against share-enabled models, with row-level security on and off.

`mine` holds a read-only share of one of their folders. Their other folder is unshared, and one
is shared with a third tenant only.
"""

import pytest
from django.db import DatabaseError, connection, transaction

from django_tenant_domains.context import no_tenant, tenant_context
from django_tenant_domains.exceptions import TenantMismatch
from django_tenant_domains.models import Tenant
from django_tenant_domains.sharing import READ, share
from tests.support import ALL_DATABASES, BYPASS, POSTGRES
from tests.testapp.models import File, Folder, FolderShare

pytestmark = pytest.mark.django_db(transaction=True, databases=ALL_DATABASES)
needs_rls = pytest.mark.skipif(not POSTGRES, reason="needs row-level security")
SHARED, HIDDEN, OTHERS = "their-shared", "their-hidden", "their-others"


@pytest.fixture
def shared(world):
    theirs = world.theirs.tenant
    from django_tenant_domains.context import _State, _switch

    previous = _switch(_State(tenant=None))
    try:
        third = Tenant.objects.create(slug="third", name="third")
        with tenant_context(theirs):
            folder = Folder.objects.create(name=SHARED)
            File.objects.create(title=f"{SHARED}-file", folder=folder)
            hidden = Folder.objects.create(name=HIDDEN)
            File.objects.create(title=f"{HIDDEN}-file", folder=hidden)
            others = Folder.objects.create(name=OTHERS)
            File.objects.create(title=f"{OTHERS}-file", folder=others)
            share(folder, world.mine.tenant, READ)
            share(others, third, READ)
    finally:
        _switch(previous)
    return world


def stored():
    return sorted(Folder.unscoped_objects.using(BYPASS).values_list("name", "tenant__slug"))


def attempt(action):
    before = stored()
    try:
        with transaction.atomic():
            action()
    except (TenantMismatch, DatabaseError):
        pass
    assert stored() == before


READS = {
    "all": lambda: Folder.objects.all(),
    "filter by name": lambda: Folder.objects.filter(name__in=[HIDDEN, OTHERS, SHARED]),
    "filter by owner": lambda: Folder.objects.filter(tenant__slug="theirs"),
    "or": lambda: Folder.objects.filter(name=HIDDEN) | Folder.objects.all(),
    "through a child": lambda: Folder.objects.filter(files__title__contains="their"),
    "union": lambda: Folder.objects.all().union(Folder.objects.filter(name=HIDDEN)),
}


@pytest.mark.parametrize("name", READS)
def test_reads_show_only_the_shared_folder(shared, name):
    assert {f.name for f in READS[name]()} == {SHARED}


def test_children_follow_the_share_only(shared):
    titles = set(File.objects.values_list("title", flat=True))
    assert titles == {f"{SHARED}-file"}


def test_count_values_and_get(shared):
    assert Folder.objects.count() == 1
    assert list(Folder.objects.values_list("name", flat=True)) == [SHARED]
    with pytest.raises(Folder.DoesNotExist):
        Folder.objects.get(name=HIDDEN)


def test_share_table_shows_only_my_share(shared):
    assert [row.record.name for row in FolderShare.objects.all()] == [SHARED]


WRITES = {
    "update": lambda: Folder.objects.all().update(name="stolen"),
    "update owner": lambda w: Folder.objects.all().update(tenant=w.mine.tenant),
    "delete": lambda: Folder.objects.all().delete(),
    "plant for them": lambda w: Folder.objects.create(name="planted", tenant=w.theirs.tenant),
}


def _call(action, world):
    return action(world) if action.__code__.co_argcount else action()


@pytest.mark.parametrize("name", WRITES)
def test_queryset_writes_change_nothing(shared, name):
    attempt(lambda: _call(WRITES[name], shared))


def test_saving_the_shared_folder_changes_nothing(shared):
    def action():
        folder = Folder.objects.get(name=SHARED)
        folder.name = "stolen"
        folder.save()

    attempt(action)


def test_taking_the_shared_folder_changes_nothing(shared):
    def action():
        folder = Folder.objects.get(name=SHARED)
        folder.tenant = shared.mine.tenant
        folder.save()

    attempt(action)


def test_forged_pk_of_a_hidden_folder_changes_nothing(shared):
    with no_tenant():
        hidden = Folder.unscoped_objects.using(BYPASS).get(name=HIDDEN)

    def action():
        Folder(pk=hidden.pk, name="stolen", tenant=shared.mine.tenant).save()

    attempt(action)


def test_share_cannot_be_upgraded(shared):
    def action():
        found = FolderShare.objects.get()
        found.access = "write"
        found.save()

    attempt(action)
    assert FolderShare.unscoped_objects.using(BYPASS).filter(access="write").count() == 0


@needs_rls
@pytest.mark.parametrize(
    "sql,rows",
    [
        ("SELECT name FROM testapp_folder WHERE tenant_id <> %s", [(SHARED,)]),
    ],
)
def test_raw_reads_show_only_the_share(shared, sql, rows):
    with connection.cursor() as cursor:
        cursor.execute(sql, [shared.mine.tenant.pk])
        assert cursor.fetchall() == rows


@needs_rls
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE testapp_folder SET name = 'stolen' WHERE tenant_id <> %s",
        "DELETE FROM testapp_folder WHERE tenant_id <> %s",
        "UPDATE testapp_foldershare SET access = 'write' WHERE owner_id <> %s",
        "DELETE FROM testapp_foldershare WHERE owner_id <> %s",
    ],
)
def test_raw_writes_change_nothing(shared, sql):
    with connection.cursor() as cursor:
        cursor.execute(sql, [shared.mine.tenant.pk])
        assert cursor.rowcount == 0
