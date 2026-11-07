"""Acme owns; Globex is the grantee; Initech is a third tenant. Runs with RLS on and off."""

import threading

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.checks import run_checks
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, router, transaction
from django.db.models import QuerySet
from django.db.models.deletion import Collector

from django_tenant_domains import managers
from django_tenant_domains.context import no_tenant, tenant_context
from django_tenant_domains.exceptions import TenantMismatch, TenantNotSet
from django_tenant_domains.models import Membership, Role, Tenant
from django_tenant_domains.permissions import shield_restricted_superuser
from django_tenant_domains.sharing import (
    READ,
    WRITE,
    AbstractShare,
    SharedRecordInUse,
    share,
    unshare,
)
from tests.support import ALL_DATABASES, BYPASS, POSTGRES
from tests.testapp.models import (
    Album,
    AlbumShare,
    Binder,
    Card,
    File,
    FileProxy,
    Folder,
    FolderProxy,
    FolderShare,
    HiddenLink,
    KeyedChild,
    KeyedLink,
    KeyedParent,
    Notebook,
    Page,
    PageNote,
    Photo,
    Sheet,
    Tag,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=ALL_DATABASES)

needs_rls = pytest.mark.skipif(not POSTGRES, reason="needs row-level security")


def peek(model):
    return model.unscoped_objects.using(BYPASS)


class World:
    def __init__(self):
        self.acme = Tenant.objects.create(slug="acme", name="Acme")
        self.globex = Tenant.objects.create(slug="globex", name="Globex")
        self.initech = Tenant.objects.create(slug="initech", name="Initech")
        with tenant_context(self.acme):
            self.folder = Folder.objects.create(name="acme-folder")
            self.file = File.objects.create(title="acme-file", folder=self.folder)
            self.private = Folder.objects.create(name="acme-private")
            self.album = Album.objects.create(name="acme-album")
            self.photo = Photo.objects.create(title="acme-photo", album=self.album)
        with tenant_context(self.globex):
            self.their_folder = Folder.objects.create(name="globex-folder")

    def grant(self, access=READ, record=None, grantee=None):
        with tenant_context(self.acme):
            return share(record or self.folder, grantee or self.globex, access)


@pytest.fixture
def world():
    return World()


def refused():
    class Refused:
        def __enter__(self):
            self.raises = pytest.raises((TenantMismatch, DatabaseError))
            self.atomic = transaction.atomic()
            self.raises.__enter__()
            self.atomic.__enter__()

        def __exit__(self, *exc):
            handled = self.atomic.__exit__(*exc)
            return self.raises.__exit__(*exc) or handled

    return Refused()


def names(model, tenant):
    with tenant_context(tenant):
        field = "name" if model in (Folder, Album) else "title"
        return set(model.objects.values_list(field, flat=True))


class TestReads:
    def test_unshared_record_is_hidden(self, world):
        assert names(Folder, world.globex) == {"globex-folder"}

    def test_grantee_sees_the_shared_record_only(self, world):
        world.grant()
        assert names(Folder, world.globex) == {"globex-folder", "acme-folder"}

    def test_third_tenant_sees_nothing(self, world):
        world.grant()
        assert names(Folder, world.initech) == set()

    def test_owner_still_sees_it(self, world):
        world.grant()
        assert names(Folder, world.acme) == {"acme-folder", "acme-private"}

    def test_owned_leaves_out_shared_records(self, world):
        world.grant()
        with tenant_context(world.globex):
            owned = set(Folder.objects.owned().values_list("name", flat=True))
        assert owned == {"globex-folder"}

    def test_revoked_share_stops_access(self, world):
        world.grant()
        with tenant_context(world.acme):
            unshare(world.folder, world.globex)
        assert names(Folder, world.globex) == {"globex-folder"}
        assert names(File, world.globex) == set()

    def test_no_tenant_still_raises(self, world):
        with pytest.raises(TenantNotSet):
            list(Folder.objects.all())

    @needs_rls
    def test_raw_sql_follows_the_share(self, world):
        world.grant()
        with tenant_context(world.globex), connection.cursor() as cursor:
            cursor.execute("SELECT name FROM testapp_folder")
            assert {row[0] for row in cursor.fetchall()} == {"globex-folder", "acme-folder"}
        with tenant_context(world.initech), connection.cursor() as cursor:
            cursor.execute("SELECT name FROM testapp_folder")
            assert cursor.fetchall() == []


class TestCascade:
    def test_child_inherits_when_declared(self, world):
        world.grant()
        assert names(File, world.globex) == {"acme-file"}

    def test_child_does_not_inherit_when_not_declared(self, world):
        world.grant(record=world.album)
        assert names(Album, world.globex) == {"acme-album"}
        assert names(Photo, world.globex) == set()

    def test_related_manager_shows_inherited_children(self, world):
        world.grant()
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            assert [item.title for item in folder.files.all()] == ["acme-file"]

    def test_write_on_child_follows_the_parent_share(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            item = File.objects.get()
            item.title = "changed"
            with refused():
                item.save()
        world.grant(WRITE)
        with tenant_context(world.globex):
            item = File.objects.get()
            item.title = "changed"
            item.save()
        stored = peek(File).get(pk=world.file.pk)
        assert (stored.title, stored.tenant) == ("changed", world.acme)

    def test_other_grantee_child_on_the_same_parent_does_not_cascade(self, world):
        """Globex's file is Globex's, even though it hangs off a folder Initech also has."""
        world.grant(WRITE)
        world.grant(READ, grantee=world.initech)
        with no_tenant():
            File.unscoped_objects.using(BYPASS).create(
                title="globex-own", folder_id=world.folder.pk, tenant=world.globex
            )
        assert names(File, world.initech) == {"acme-file"}
        assert names(File, world.acme) == {"acme-file"}
        assert names(File, world.globex) == {"acme-file", "globex-own"}

    @needs_rls
    def test_raw_read_of_child_cannot_cross_grantees(self, world):
        world.grant(READ)
        world.grant(READ, grantee=world.initech)
        with no_tenant():
            File.unscoped_objects.using(BYPASS).create(
                title="globex-own", folder_id=world.folder.pk, tenant=world.globex
            )
        with tenant_context(world.initech), connection.cursor() as cursor:
            cursor.execute("SELECT title FROM testapp_file")
            assert {row[0] for row in cursor.fetchall()} == {"acme-file"}


class TestReadOnlyBlocksEveryWrite:
    @pytest.fixture(autouse=True)
    def _grant(self, world):
        world.grant(READ)

    def _unchanged(self, world):
        stored = peek(Folder).get(pk=world.folder.pk)
        assert (stored.name, stored.tenant) == ("acme-folder", world.acme)

    def test_save(self, world):
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            folder.name = "changed"
            with refused():
                folder.save()
        self._unchanged(world)

    def test_delete(self, world):
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            with refused():
                folder.delete()
        self._unchanged(world)

    def test_queryset_update(self, world):
        with tenant_context(world.globex), refused():
            Folder.objects.all().update(name="changed")
        self._unchanged(world)
        assert peek(Folder).get(pk=world.their_folder.pk).name == "globex-folder"

    def test_bulk_update(self, world):
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            folder.name = "changed"
            with refused():
                Folder.objects.bulk_update([folder], ["name"])
        self._unchanged(world)

    def test_queryset_delete(self, world):
        with tenant_context(world.globex), refused():
            Folder.objects.all().delete()
        self._unchanged(world)

    @needs_rls
    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE testapp_folder SET name = 'changed' WHERE name = 'acme-folder'",
            "DELETE FROM testapp_folder WHERE name = 'acme-folder'",
        ],
    )
    def test_raw_sql_changes_nothing(self, world, sql):
        with tenant_context(world.globex), connection.cursor() as cursor:
            cursor.execute(sql)
            assert cursor.rowcount == 0
        self._unchanged(world)


class TestReadWrite:
    @pytest.fixture(autouse=True)
    def _grant(self, world):
        world.grant(WRITE)

    def test_save_edits_and_owner_stays(self, world):
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            folder.name = "edited"
            folder.save()
        stored = peek(Folder).get(pk=world.folder.pk)
        assert (stored.name, stored.tenant) == ("edited", world.acme)

    def test_queryset_update_of_a_shared_row(self, world):
        with tenant_context(world.globex):
            assert Folder.objects.filter(name="acme-folder").update(name="edited") == 1
        assert peek(Folder).get(pk=world.folder.pk).name == "edited"

    def test_bulk_update(self, world):
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            folder.name = "edited"
            Folder.objects.bulk_update([folder], ["name"])
        assert peek(Folder).get(pk=world.folder.pk).name == "edited"

    def test_cannot_change_the_owner_by_save(self, world):
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            folder.tenant_id = world.globex.pk
            with refused():
                folder.save()
        assert peek(Folder).get(pk=world.folder.pk).tenant == world.acme

    def test_cannot_change_the_owner_by_update(self, world):
        with tenant_context(world.globex), refused():
            Folder.objects.filter(name="acme-folder").update(tenant=world.globex)
        assert peek(Folder).get(pk=world.folder.pk).tenant == world.acme

    def test_cannot_delete(self, world):
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            with refused():
                folder.delete()
            with refused():
                Folder.objects.filter(name="acme-folder").delete()
        assert peek(Folder).filter(pk=world.folder.pk).exists()

    @pytest.mark.parametrize("field", ["id", "pk"])
    def test_cannot_change_a_primary_key_by_update(self, world, field):
        with tenant_context(world.globex), refused():
            File.objects.filter(pk=world.file.pk).update(**{field: world.file.pk + 1000})
        assert peek(File).filter(pk=world.file.pk).exists()

    def test_reverse_add_moves_a_child_between_shared_parents(self, world):
        world.grant(WRITE)
        world.grant(WRITE, record=world.private)
        with tenant_context(world.globex):
            target = Folder.objects.get(name="acme-private")
            target.files.add(File.objects.get(pk=world.file.pk))
        assert peek(File).get(pk=world.file.pk).folder_id == world.private.pk

    def test_reverse_add_is_refused_on_a_read_only_child(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            child = File.objects.get(pk=world.file.pk)
            with refused():
                world.their_folder.files.add(child)
        assert peek(File).get(pk=world.file.pk).folder_id == world.folder.pk

    def test_cannot_touch_unshared_records(self, world):
        with tenant_context(world.globex):
            assert Folder.objects.filter(name="acme-private").update(name="x") == 0
        assert peek(Folder).get(pk=world.private.pk).name == "acme-private"

    def test_foreign_key_on_a_shared_row_must_suit_the_owner(self, world):
        with tenant_context(world.globex):
            item = File.objects.get()
            item.folder_id = world.their_folder.pk
            with refused():
                item.save()
            with refused():
                File.objects.filter(pk=item.pk).update(folder_id=world.their_folder.pk)
        assert peek(File).get(pk=world.file.pk).folder_id == world.folder.pk

    def test_update_spanning_two_owners_with_a_key_is_refused(self, world):
        with tenant_context(world.globex):
            File.objects.create(title="mine", folder=world.their_folder)
            with refused():
                File.objects.all().update(folder_id=world.folder.pk)

    def test_update_without_a_key_spanning_two_owners_works(self, world):
        with tenant_context(world.globex):
            File.objects.create(title="mine", folder=world.their_folder)
            assert File.objects.all().update(title="same") == 2

    @needs_rls
    def test_raw_update_works_but_cannot_take_ownership(self, world):
        with tenant_context(world.globex), connection.cursor() as cursor:
            cursor.execute("UPDATE testapp_folder SET name = 'raw' WHERE name = 'acme-folder'")
            assert cursor.rowcount == 1
        for sql in (
            "UPDATE testapp_folder SET tenant_id = %s WHERE name = 'raw'",
            "UPDATE testapp_folder SET id = id + 1000 + %s WHERE name = 'raw'",
        ):
            with (
                tenant_context(world.globex),
                pytest.raises(DatabaseError),
                connection.cursor() as cursor,
            ):
                cursor.execute(sql, [world.globex.pk])
        stored = peek(Folder).get(pk=world.folder.pk)
        assert (stored.name, stored.tenant) == ("raw", world.acme)

    @needs_rls
    def test_temporary_table_cannot_fool_the_owner_trigger(self, world):
        with tenant_context(world.globex), connection.cursor() as cursor:
            cursor.execute(
                "CREATE TEMP TABLE pg_roles ON COMMIT DROP AS "
                "SELECT current_user AS rolname, true AS rolsuper, true AS rolbypassrls"
            )
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    "UPDATE testapp_folder SET tenant_id = %s WHERE name = 'acme-folder'",
                    [world.globex.pk],
                )
        assert peek(Folder).get(pk=world.folder.pk).tenant == world.acme

    @needs_rls
    def test_raw_delete_changes_nothing(self, world):
        with tenant_context(world.globex), connection.cursor() as cursor:
            cursor.execute("DELETE FROM testapp_folder WHERE name = 'acme-folder'")
            assert cursor.rowcount == 0

    @needs_rls
    def test_raw_insert_for_the_owner_is_refused(self, world):
        with (
            tenant_context(world.globex),
            pytest.raises(DatabaseError),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO testapp_folder (name, tenant_id) VALUES ('x', %s)", [world.acme.pk]
            )

    def test_new_record_cannot_be_created_for_the_owner(self, world):
        with tenant_context(world.globex), refused():
            Folder.objects.create(name="planted", tenant=world.acme)


class TestAlternateKeys:
    """A share is stored by primary key, and must not match a parent whose other key is equal."""

    def _parents(self, world):
        with tenant_context(world.acme):
            return (
                KeyedParent.objects.create(code=world.private.pk),
                KeyedParent.objects.create(code=world.folder.pk),
            )

    def test_child_of_an_alternate_key_does_not_inherit(self, world):
        first, second = self._parents(world)
        with tenant_context(world.acme):
            KeyedChild.objects.create(parent=first)
            KeyedChild.objects.create(parent=second)
            share(first, world.globex, WRITE)
        with tenant_context(world.globex):
            assert not KeyedChild.objects.exists()

    def test_alternate_key_cannot_link(self, world):
        first, second = self._parents(world)
        with tenant_context(world.acme):
            share(second, world.globex, READ)
        with tenant_context(world.globex), refused():
            KeyedLink.objects.create(parent_id=first.code)


class TestForgedAttributes:
    def test_forged_owner_cannot_pass_the_link_check_on_delete(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            Card.objects.create(title="mine", folder_id=world.folder.pk)
        with tenant_context(world.acme):
            folder = Folder.objects.get(name="acme-folder")
            folder.tenant_id = world.globex.pk
            with refused():
                folder.delete()
        assert peek(Card).exists()

    def test_forged_tag_cannot_clear_the_owners_links(self, world):
        with tenant_context(world.acme):
            tag = Tag.objects.create(name="acme-tag")
            Folder.objects.get(name="acme-folder").tags.add(tag)
        world.grant(READ)
        with tenant_context(world.globex):
            forged = Tag(pk=tag.pk, name="x", tenant=world.globex)
            with refused():
                forged.folder_set.clear()
        assert Folder.tags.through.objects.using(BYPASS).count() == 1


class TestUnchangedKeys:
    def test_grantee_can_save_a_shared_row_whose_key_it_cannot_see(self, world):
        with tenant_context(world.acme):
            tag = Tag.objects.create(name="private-tag")
            sheet = Sheet.objects.create(title="sheet", tag=tag)
            share(sheet, world.globex, WRITE)
        with tenant_context(world.globex):
            found = Sheet.objects.get()
            found.title = "edited"
            found.save()
        stored = peek(Sheet).get(pk=sheet.pk)
        assert (stored.title, stored.tag_id) == ("edited", tag.pk)

    def test_full_clean_passes_on_a_key_it_cannot_see(self, world):
        with tenant_context(world.acme):
            tag = Tag.objects.create(name="private-tag")
            sheet = Sheet.objects.create(title="sheet", tag=tag)
            share(sheet, world.globex, WRITE)
        with tenant_context(world.globex):
            found = Sheet.objects.get()
            found.title = "edited"
            found.full_clean()

    def test_full_clean_passes_on_a_kept_link_and_checks_a_changed_one(self, world):
        card = self._kept_card(world)
        with tenant_context(world.globex):
            found = Card.objects.get(pk=card.pk)
            found.full_clean()
            found.folder_id = world.private.pk
            with pytest.raises(ValidationError):
                found.full_clean()

    def test_full_clean_still_runs_custom_validators_on_a_kept_key(self, world, monkeypatch):
        def refuse(value):
            raise ValidationError("no")

        card = self._kept_card(world)
        field = Card._meta.get_field("folder")
        monkeypatch.setattr(field, "validators", [refuse])
        with tenant_context(world.globex):
            found = Card.objects.get(pk=card.pk)
            with pytest.raises(ValidationError) as caught:
                found.full_clean()
        assert "folder" in caught.value.error_dict

    def test_full_clean_converts_a_kept_key_before_validating(self, world, monkeypatch):
        seen = []
        card = self._kept_card(world)
        monkeypatch.setattr(Card._meta.get_field("folder"), "validators", [seen.append])
        with tenant_context(world.globex):
            found = Card.objects.get(pk=card.pk)
            found.folder_id = world.folder.pk
            found.full_clean()
        assert seen == [world.folder.pk]

    def test_changing_that_key_is_still_checked(self, world):
        with tenant_context(world.globex):
            their_tag = Tag.objects.create(name="their-tag")
        with tenant_context(world.acme):
            tag = Tag.objects.create(name="private-tag")
            sheet = Sheet.objects.create(title="sheet", tag=tag)
            share(sheet, world.globex, WRITE)
        with tenant_context(world.globex):
            found = Sheet.objects.get()
            found.tag_id = their_tag.pk
            with refused():
                found.save()
        assert peek(Sheet).get(pk=sheet.pk).tag_id == tag.pk

    def _kept_card(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            card = Card.objects.create(title="mine", folder_id=world.folder.pk)
        with tenant_context(world.acme):
            unshare(world.folder, world.globex)
        return card

    def test_bulk_update_keeps_an_unchanged_link_after_the_share_ends(self, world):
        card = self._kept_card(world)
        with tenant_context(world.globex):
            found = Card.objects.get(pk=card.pk)
            found.title = "renamed"
            Card.objects.bulk_update([found], ["title", "folder"])
        assert peek(Card).get(pk=card.pk).title == "renamed"

    def test_update_keeps_an_unchanged_link_after_the_share_ends(self, world):
        card = self._kept_card(world)
        with tenant_context(world.globex):
            Card.objects.filter(pk=card.pk).update(title="renamed", folder_id=world.folder.pk)
        assert peek(Card).get(pk=card.pk).title == "renamed"

    def test_bulk_update_and_update_still_check_a_changed_link(self, world):
        card = self._kept_card(world)
        with tenant_context(world.globex):
            found = Card.objects.get(pk=card.pk)
            found.folder_id = world.private.pk
            with refused():
                Card.objects.bulk_update([found], ["folder"])
            with refused():
                Card.objects.filter(pk=card.pk).update(folder_id=world.private.pk)
        assert peek(Card).get(pk=card.pk).folder_id == world.folder.pk

    def test_update_over_rows_with_mixed_keys_is_still_checked(self, world):
        card = self._kept_card(world)
        with tenant_context(world.globex):
            Card.objects.create(title="other", folder=world.their_folder)
            with refused():
                Card.objects.all().update(folder_id=world.folder.pk)
        assert peek(Card).get(pk=card.pk).folder_id == world.folder.pk

    def test_bulk_update_across_owners_keeps_each_rows_key(self, world):
        world.grant(WRITE)
        with tenant_context(world.globex):
            File.objects.create(title="mine", folder=world.their_folder)
            rows = list(File.objects.all())
            for row in rows:
                row.title = "same"
            File.objects.bulk_update(rows, ["title", "folder"])
            assert set(File.objects.values_list("title", flat=True)) == {"same"}


class TestWriteChecksUseTheWriteDatabase:
    @pytest.fixture
    def aliases(self, monkeypatch):
        seen = []
        original = QuerySet.using

        def spy(self, alias):
            seen.append(alias)
            return original(self, alias)

        monkeypatch.setattr(QuerySet, "using", spy)
        return seen

    def test_share_save_checks_a_positional_alias(self, world, aliases):
        world.grant(READ)
        with tenant_context(world.globex):
            found = FolderShare.objects.get()
            found.access = WRITE
            with pytest.raises(TenantMismatch):
                found.save(False, False, BYPASS, ["access"])
        assert BYPASS in aliases

    @pytest.mark.parametrize(
        "action",
        [
            lambda world: unshare(world.folder, world.globex),
            lambda world: FolderShare.objects.all().delete(),
        ],
    )
    def test_ending_a_share_reads_the_write_database(self, world, aliases, monkeypatch, action):
        world.grant(READ)
        monkeypatch.setattr(router, "db_for_write", lambda *args, **kwargs: BYPASS)
        with tenant_context(world.globex), pytest.raises(TenantMismatch):
            action(world)
        assert BYPASS in aliases

    def test_checks_follow_the_write_alias(self, world, monkeypatch):
        """A replica can lag behind a downgrade, so these checks must ask the primary."""
        world.grant(READ)
        seen = []
        original = QuerySet.using

        def spy(self, alias):
            seen.append(alias)
            return original(self, alias)

        monkeypatch.setattr(QuerySet, "using", spy)
        monkeypatch.setattr(managers, "_write_db", lambda queryset: "default")
        with tenant_context(world.globex), refused():
            Folder.objects.filter(name="acme-folder").update(name="x")
        assert "default" in seen


class TestBulkPaths:
    def test_bulk_update_cannot_forge_the_owner_to_pass_the_key_check(self, world):
        with tenant_context(world.globex):
            mine = File.objects.create(title="mine", folder=world.their_folder)
            mine.tenant_id = world.acme.pk
            mine.folder_id = world.private.pk
            with refused():
                File.objects.bulk_update([mine], ["folder"])
        assert peek(File).get(pk=mine.pk).folder_id == world.their_folder.pk

    def test_owned_bulk_update_skips_shared_rows_it_filters_out(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            rows = list(Folder.objects.all())
            for row in rows:
                row.name = "renamed"
            assert Folder.objects.owned().bulk_update(rows, ["name"]) == 1
        assert peek(Folder).get(pk=world.their_folder.pk).name == "renamed"
        assert peek(Folder).get(pk=world.folder.pk).name == "acme-folder"

    def test_owned_bulk_update_checks_keys_only_on_rows_it_reaches(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            File.objects.create(title="mine", folder=world.their_folder)
            other = Folder.objects.create(name="other")
            rows = list(File.objects.all())
            for row in rows:
                row.folder_id = other.pk
            assert File.objects.owned().bulk_update(rows, ["folder"]) == 1
        assert peek(File).get(pk=world.file.pk).folder_id == world.folder.pk

    def test_upsert_on_a_shared_model_is_refused(self, world):
        with tenant_context(world.globex), refused():
            Folder.objects.bulk_create(
                [Folder(pk=world.private.pk, name="taken")],
                update_conflicts=True,
                update_fields=["name", "tenant"],
                unique_fields=["id"],
            )
        stored = peek(Folder).get(pk=world.private.pk)
        assert (stored.name, stored.tenant) == ("acme-private", world.acme)

    def test_upsert_by_position_is_refused(self, world):
        with tenant_context(world.globex), refused():
            Folder.objects.bulk_create(
                [Folder(pk=world.private.pk, name="taken")],
                None,
                False,
                True,
                ["name", "tenant"],
                ["id"],
            )
        stored = peek(Folder).get(pk=world.private.pk)
        assert (stored.name, stored.tenant) == ("acme-private", world.acme)

    def test_upsert_cannot_change_the_owner_on_any_model(self, world):
        with tenant_context(world.globex), refused():
            Tag.objects.bulk_create(
                [Tag(name="x")],
                update_conflicts=True,
                update_fields=["tenant"],
                unique_fields=["id"],
            )


class TestShareRules:
    @pytest.mark.postgres
    @needs_rls
    def test_simultaneous_grants_both_succeed(self, world, monkeypatch):
        """Both calls find no share, then both insert. The second must become an update."""
        barrier = threading.Barrier(2, timeout=10)
        save = AbstractShare.save
        waited = threading.local()

        def save_after_both_looked(self, *args, **kwargs):
            if self.pk is None and not getattr(waited, "done", False):
                waited.done = True
                barrier.wait()
            return save(self, *args, **kwargs)

        monkeypatch.setattr(AbstractShare, "save", save_after_both_looked)
        errors = []

        def grant():
            try:
                with tenant_context(world.acme):
                    share(world.folder, world.globex, WRITE)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=grant) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert list(peek(FolderShare).values_list("access", flat=True)) == [WRITE]

    def test_only_the_owner_can_share(self, world):
        with tenant_context(world.globex), refused():
            share(world.folder, world.globex, WRITE)
        assert not peek(FolderShare).exists()

    def test_grantee_cannot_upgrade_itself(self, world):
        world.grant(READ)
        with tenant_context(world.globex), refused():
            share(world.folder, world.globex, WRITE)
        assert peek(FolderShare).get().access == READ

    def test_grantee_cannot_pass_it_on(self, world):
        world.grant(WRITE)
        with tenant_context(world.globex), refused():
            share(world.folder, world.initech, WRITE)
        assert peek(FolderShare).count() == 1

    def test_grantee_cannot_end_it_or_edit_it(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            found = FolderShare.objects.get()
            found.access = WRITE
            with refused():
                found.save()
            with refused():
                found.delete()
            with refused():
                FolderShare.objects.all().delete()
            with refused():
                FolderShare.objects.all().update(access=WRITE)
            with refused():
                FolderShare.objects.bulk_create(
                    [FolderShare(record=world.private, grantee=world.globex, owner=world.acme)]
                )
        assert peek(FolderShare).get().access == READ

    def test_grantee_cannot_upgrade_with_replacement_fields(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            found = FolderShare.objects.get()
            found.record_id = world.their_folder.pk
            found.grantee = world.initech
            found.access = WRITE
            with refused():
                found.save(update_fields=["access"])
        assert peek(FolderShare).get().access == READ

    def test_grantee_cannot_end_it_with_a_forged_owner(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            found = FolderShare.objects.get()
            found.owner_id = world.globex.pk
            with refused():
                found.delete()
        assert peek(FolderShare).exists()

    def test_cannot_share_with_itself(self, world):
        with tenant_context(world.acme), refused():
            share(world.folder, world.acme)

    def test_sharing_twice_changes_the_access(self, world):
        world.grant(READ)
        world.grant(WRITE)
        assert peek(FolderShare).get().access == WRITE

    def test_third_tenant_cannot_see_shares(self, world):
        world.grant(READ)
        with tenant_context(world.initech):
            assert not FolderShare.objects.exists()

    def test_share_manager_needs_a_tenant(self):
        with pytest.raises(TenantNotSet):
            FolderShare.objects.count()

    def test_unknown_access_is_refused(self, world):
        with tenant_context(world.acme), pytest.raises(ValueError):
            share(world.folder, world.globex, "admin")

    @needs_rls
    def test_raw_self_grant_is_refused(self, world):
        sql = (
            "INSERT INTO testapp_foldershare (record_id, owner_id, grantee_id, access, created_at) "
            "VALUES (%s, %s, %s, 'write', now())"
        )
        forged = [
            [world.folder.pk, world.globex.pk, world.globex.pk],
            [world.folder.pk, world.acme.pk, world.globex.pk],
        ]
        for params in forged:
            with (
                tenant_context(world.globex),
                pytest.raises(DatabaseError),
                connection.cursor() as cursor,
            ):
                cursor.execute(sql, params)
        with (
            tenant_context(world.globex),
            pytest.raises(DatabaseError),
            connection.cursor() as cursor,
        ):
            third = Tenant.objects.get(slug="initech")
            cursor.execute(sql, [world.folder.pk, world.globex.pk, third.pk])
        assert not peek(FolderShare).exists()

    @needs_rls
    def test_raw_upgrade_of_own_grant_is_refused(self, world):
        world.grant(READ)
        with tenant_context(world.globex), connection.cursor() as cursor:
            cursor.execute("UPDATE testapp_foldershare SET access = 'write'")
            assert cursor.rowcount == 0
        assert peek(FolderShare).get().access == READ

    @needs_rls
    def test_raw_forged_owner_on_my_share_cannot_grant_their_record(self, world):
        sql = "UPDATE testapp_foldershare SET record_id = %s"
        with tenant_context(world.acme):
            share(world.folder, world.globex, READ)
        with (
            tenant_context(world.initech),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql, [world.folder.pk])
            assert cursor.rowcount == 0


class TestLinks:
    def test_grantee_can_link_when_allowed(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            card = Card.objects.create(title="mine", folder_id=world.folder.pk)
        stored = peek(Card).get(pk=card.pk)
        assert (stored.tenant, stored.folder_id) == (world.globex, world.folder.pk)

    def test_cannot_link_to_an_unshared_record(self, world):
        with tenant_context(world.globex), refused():
            Card.objects.create(title="mine", folder_id=world.folder.pk)

    def test_third_tenant_cannot_link(self, world):
        world.grant(READ)
        with tenant_context(world.initech), refused():
            Card.objects.create(title="mine", folder_id=world.folder.pk)

    def test_cannot_link_when_the_model_does_not_opt_in(self, world):
        world.grant(WRITE, record=world.album)
        with tenant_context(world.globex), refused():
            Photo.objects.create(title="mine", album_id=world.album.pk)

    def test_owner_does_not_see_the_grantees_linked_record(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            Card.objects.create(title="mine", folder_id=world.folder.pk)
        with tenant_context(world.acme):
            assert not Card.objects.exists()

    @needs_rls
    def test_joining_a_hidden_link_hides_the_row_so_select_related_is_documented(self, world):
        card = self._linked(world)
        with tenant_context(world.acme):
            unshare(world.folder, world.globex)
        with tenant_context(world.globex):
            assert Card.objects.filter(pk=card.pk).exists()
            assert list(Card.objects.select_related("folder").filter(pk=card.pk)) == []

    def test_link_survives_revoke_but_the_record_is_unreadable(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            card = Card.objects.create(title="mine", folder_id=world.folder.pk)
        with tenant_context(world.acme):
            unshare(world.folder, world.globex)
        with tenant_context(world.globex):
            stored = Card.objects.get(pk=card.pk)
            assert stored.folder_id == world.folder.pk
            assert not Folder.objects.filter(pk=world.folder.pk).exists()
            if POSTGRES:
                with pytest.raises(Folder.DoesNotExist):
                    stored.folder  # noqa: B018

    def test_grantee_keeps_its_own_cascaded_child_after_the_share_ends(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            mine = File.objects.create(title="mine", folder_id=world.folder.pk)
        with tenant_context(world.acme):
            unshare(world.folder, world.globex)
        with tenant_context(world.globex):
            assert list(File.objects.values_list("pk", flat=True)) == [mine.pk]
            assert list(File.objects.owned().values_list("pk", flat=True)) == [mine.pk]

    def _linked(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            return Card.objects.create(title="mine", folder_id=world.folder.pk)

    def test_owner_cannot_delete_a_linked_record(self, world):
        self._linked(world)
        with tenant_context(world.acme):
            folder = Folder.objects.get(name="acme-folder")
            with pytest.raises(SharedRecordInUse):
                folder.delete()
        assert peek(Folder).filter(pk=world.folder.pk).exists()
        assert peek(Card).exists()

    def test_owner_cannot_queryset_delete_a_linked_record(self, world):
        self._linked(world)
        with tenant_context(world.acme), pytest.raises(SharedRecordInUse):
            Folder.objects.filter(name="acme-folder").delete()
        assert peek(Card).exists()

    def test_no_tenant_delete_through_the_bypass_login_is_refused(self, world):
        self._linked(world)
        with no_tenant(), pytest.raises(SharedRecordInUse):
            Folder.unscoped_objects.using(BYPASS).filter(pk=world.folder.pk).delete()
        assert peek(Card).exists()

    def test_owner_can_delete_after_the_link_is_cleared(self, world):
        card = self._linked(world)
        with tenant_context(world.globex):
            Card.objects.filter(pk=card.pk).delete()
        with tenant_context(world.acme):
            Folder.objects.get(name="acme-folder").delete()
        assert not peek(Folder).filter(pk=world.folder.pk).exists()

    def test_owner_can_delete_a_shared_record_with_no_links(self, world):
        world.grant(READ)
        with tenant_context(world.acme):
            Folder.objects.get(name="acme-folder").delete()
        assert not peek(FolderShare).exists()
        assert not peek(File).filter(pk=world.file.pk).exists()

    def test_grantee_can_still_save_its_record_after_the_share_ends(self, world):
        card = self._linked(world)
        with tenant_context(world.acme):
            unshare(world.folder, world.globex)
        with tenant_context(world.globex):
            stored = Card.objects.get(pk=card.pk)
            stored.title = "renamed"
            stored.save()
        saved = peek(Card).get(pk=card.pk)
        assert (saved.title, saved.folder_id) == ("renamed", world.folder.pk)

    def test_after_the_share_ends_the_link_cannot_be_newly_assigned(self, world):
        card = self._linked(world)
        with tenant_context(world.acme):
            unshare(world.folder, world.globex)
        with tenant_context(world.globex):
            other = Card.objects.create(title="other", folder=world.their_folder)
            other.folder_id = world.folder.pk
            with refused():
                other.save()
        assert peek(Card).get(pk=card.pk).folder_id == world.folder.pk

    def test_hidden_relation_also_blocks_delete(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            HiddenLink.objects.create(folder_id=world.folder.pk)
        with tenant_context(world.acme), pytest.raises(SharedRecordInUse):
            Folder.objects.get(name="acme-folder").delete()
        assert peek(HiddenLink).exists()

    def test_owner_own_cards_do_not_block_delete(self, world):
        with tenant_context(world.acme):
            folder = Folder.objects.get(name="acme-folder")
            Card.objects.create(title="own", folder=folder)
            folder.delete()
        assert not peek(Card).exists()


class TestManyToMany:
    def test_read_only_grantee_cannot_add_links(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            tag = Tag.objects.create(name="tag")
            folder = Folder.objects.get(name="acme-folder")
            with refused():
                folder.tags.add(tag)

    def _acme_tag(self, world):
        with tenant_context(world.acme):
            return Tag.objects.create(name="acme-tag")

    def test_grantee_cannot_link_two_of_the_owners_records(self, world):
        tag = self._acme_tag(world)
        for access in (READ, WRITE):
            world.grant(access)
            with tenant_context(world.globex):
                folder = Folder.objects.get(name="acme-folder")
                with refused():
                    folder.tags.add(tag.pk)
        assert not Folder.tags.through.objects.using(BYPASS).exists()

    def test_grantee_cannot_remove_or_clear_links(self, world):
        tag = self._acme_tag(world)
        with tenant_context(world.acme):
            Folder.objects.get(name="acme-folder").tags.add(tag)
        world.grant(WRITE)
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            with refused():
                folder.tags.clear()
            with refused():
                folder.tags.remove(tag.pk)
        assert Folder.tags.through.objects.using(BYPASS).count() == 1

    def _shared_notebook(self, world):
        user = get_user_model().objects.create_user("editor")
        with tenant_context(world.acme):
            book = Notebook.objects.create(title="book")
            book.editors.add(user)
            share(book, world.globex, WRITE)
        return user, book

    @pytest.mark.parametrize(
        "change",
        [
            lambda user, book: user.notebooks.remove(book.pk),
            lambda user, book: user.notebooks.clear(),
            lambda user, book: user.notebooks.set([]),
        ],
    )
    def test_grantee_cannot_change_links_from_the_unscoped_side(self, world, change):
        user, book = self._shared_notebook(world)
        with tenant_context(world.globex), refused():
            change(user, book)
        assert Notebook.editors.through.objects.using(BYPASS).count() == 1

    def test_grantee_cannot_add_links_from_the_unscoped_side(self, world):
        _user, book = self._shared_notebook(world)
        other = get_user_model().objects.create_user("other")
        with tenant_context(world.globex), refused():
            other.notebooks.add(book.pk)
        assert Notebook.editors.through.objects.using(BYPASS).count() == 1

    def test_owner_can_change_links_from_the_unscoped_side(self, world):
        user, book = self._shared_notebook(world)
        with tenant_context(world.acme):
            user.notebooks.remove(book.pk)
        assert Notebook.editors.through.objects.using(BYPASS).count() == 0

    def test_owner_can_still_change_links(self, world):
        tag = self._acme_tag(world)
        world.grant(READ)
        with tenant_context(world.acme):
            folder = Folder.objects.get(name="acme-folder")
            folder.tags.add(tag)
            folder.tags.remove(tag)

    @needs_rls
    def test_raw_link_row_on_a_shared_record_is_refused(self, world):
        world.grant(WRITE)
        with tenant_context(world.globex):
            tag = Tag.objects.create(name="tag")
        with (
            tenant_context(world.globex),
            pytest.raises(DatabaseError),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO testapp_folder_tags (folder_id, tag_id) VALUES (%s, %s)",
                [world.folder.pk, tag.pk],
            )


@needs_rls
class TestPolicies:
    def test_every_shared_table_has_all_its_policies(self):
        from django_tenant_domains.rls.policies import missing_policies

        assert missing_policies() == []

    def test_check_fails_when_one_split_policy_is_gone(self):
        from django.core.management import call_command

        from django_tenant_domains.rls.policies import missing_policies

        with connections["migrator"].cursor() as cursor:
            cursor.execute("DROP POLICY tenant_domains_update ON testapp_folder")
        try:
            assert missing_policies() == ["testapp_folder"]
        finally:
            call_command("tenant_rls")
        assert missing_policies() == []

    def test_share_table_is_covered(self):
        from django_tenant_domains.rls.policies import table_policies

        assert "testapp_foldershare" in [p.table for p in table_policies(connection)]
        assert AlbumShare._tenant_is_share_table


def member_role():
    # A transactional test flushes the roles seeded by the migration.
    role, _ = Role.objects.get_or_create(
        tenant=None,
        name="member",
        defaults={"permissions": ["*.view_*", "*.change_*", "*.delete_*"]},
    )
    return role


def test_sharing_leaves_fast_delete_on_for_unrelated_models():
    collector = Collector("default", origin=None)
    assert collector.can_fast_delete(Session.objects.all())


def test_an_inherited_share_allows_a_link(world):
    with tenant_context(world.acme):
        binder = Binder.objects.create(name="binder")
        page = Page.objects.create(binder=binder)
        share(binder, world.globex, READ)
    with tenant_context(world.globex):
        note = PageNote.objects.create(page_id=page.pk)
    assert peek(PageNote).get(pk=note.pk).tenant == world.globex
    with tenant_context(world.initech), refused():
        PageNote.objects.create(page_id=page.pk)


class TestProxies:
    def test_delete_through_a_proxy_is_refused_when_linked(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            Card.objects.create(title="mine", folder_id=world.folder.pk)
        with tenant_context(world.acme), pytest.raises(SharedRecordInUse):
            FolderProxy.objects.get(name="acme-folder").delete()
        assert peek(Card).exists()

    def test_proxy_of_a_cascaded_child_inherits(self, world):
        world.grant(READ)
        with tenant_context(world.globex):
            assert [item.title for item in FileProxy.objects.all()] == ["acme-file"]
            item = FileProxy.objects.get()
            item.title = "changed"
            with refused():
                item.save()


class TestObjectPermissions:
    @pytest.fixture
    def member(self, world):
        user = get_user_model().objects.create_user("member")
        Membership.objects.create(user=user, tenant=world.globex, role=member_role())
        return get_user_model().objects.get(pk=user.pk)

    @pytest.fixture
    def restricted_root(self, world):
        Tenant.objects.filter(pk=world.globex.pk).update(restrict_superusers=True)
        world.globex.refresh_from_db()
        user = get_user_model().objects.create_superuser("root")
        Membership.objects.create(user=user, tenant=world.globex, role=member_role())
        return user

    @pytest.mark.parametrize(
        "access,verb,expected",
        [
            (READ, "view", True),
            (READ, "change", False),
            (READ, "delete", False),
            (WRITE, "view", True),
            (WRITE, "change", True),
            (WRITE, "delete", False),
        ],
    )
    def test_grantee_follows_the_share(self, world, member, access, verb, expected):
        world.grant(access)
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            assert member.has_perm(f"testapp.{verb}_folder", folder) is expected

    def test_listing_permissions_follows_the_share(self, world, member):
        world.grant(READ)
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            allowed = member.get_all_permissions(folder)
            assert "testapp.view_folder" in allowed
            assert "testapp.change_folder" not in allowed
            assert member.get_all_permissions(world.private) == set()

    def test_with_perm_follows_the_share(self, world, member):
        world.grant(READ)
        backend = "django_tenant_domains.backends.TenantBackend"
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            users = get_user_model().objects
            assert member in users.with_perm("testapp.view_folder", backend=backend, obj=folder)
            assert member not in users.with_perm(
                "testapp.change_folder", backend=backend, obj=folder
            )

    def test_unshared_record_is_refused(self, world, member):
        with tenant_context(world.globex):
            assert not member.has_perm("testapp.view_folder", world.private)

    def test_restricted_superuser_follows_the_share(self, world, restricted_root):
        world.grant(READ)
        shield_restricted_superuser(restricted_root)
        with tenant_context(world.globex):
            folder = Folder.objects.get(name="acme-folder")
            assert restricted_root.has_perm("testapp.view_folder", folder)
            assert not restricted_root.has_perm("testapp.change_folder", folder)
            assert not restricted_root.has_perm("testapp.view_folder", world.private)


def test_deleting_a_user_with_no_shares_works():
    user = get_user_model().objects.create_user("gone")
    pk = user.pk
    user.delete()
    assert not get_user_model().objects.filter(pk=pk).exists()


def test_deleting_the_granter_keeps_the_share(world):
    user = get_user_model().objects.create_user("gone")
    with tenant_context(world.acme):
        share(world.folder, world.globex, READ, granted_by=user)
    with no_tenant():
        user.delete(using=BYPASS)
    assert peek(FolderShare).get().granted_by is None


def test_grantee_cannot_erase_who_granted_a_share(world):
    user = get_user_model().objects.create_user("granter")
    with tenant_context(world.acme):
        share(world.folder, world.globex, READ, granted_by=user)
    with tenant_context(world.globex), refused():
        FolderShare.objects.all().update(granted_by=None)
    assert peek(FolderShare).get().granted_by_id == user.pk


def test_share_table_guard_still_refuses_clearing_with_other_values(world):
    world.grant(READ)
    with tenant_context(world.globex), refused():
        FolderShare.objects.all().update(granted_by=None, access=WRITE)
    assert peek(FolderShare).get().access == READ


def test_clean_lets_a_read_write_grantee_validate(world):
    world.grant(WRITE)
    with tenant_context(world.globex):
        folder = Folder.objects.get(name="acme-folder")
        folder.full_clean()


def test_clean_refuses_a_read_only_grantee(world):
    from django.core.exceptions import ValidationError

    world.grant(READ)
    with tenant_context(world.globex):
        folder = Folder.objects.get(name="acme-folder")
        with pytest.raises(ValidationError):
            folder.clean()


def test_shared_models_pass_their_system_checks():
    ids = [check.id for check in run_checks()]
    assert "tenant_domains.E005" not in ids
    assert "tenant_domains.E006" not in ids
