"""Acme owns; Globex is another tenant. Runs with row-level security on and off."""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.db.models import Value
from django.test import override_settings

from django_tenant_domains import checks, external
from django_tenant_domains.context import _State, _switch, no_tenant, tenant_context
from django_tenant_domains.exceptions import TenantMismatch, TenantNotSet
from django_tenant_domains.external import (
    OWNER,
    assign_owner,
    external_records,
    link_of,
    register_external,
    share,
    unshare,
)
from django_tenant_domains.models import Membership, Role, Tenant
from django_tenant_domains.permissions import shield_restricted_superuser
from django_tenant_domains.rls import SETTING_NAME
from django_tenant_domains.rls.policies import (
    TablePolicy,
    _located,
    _owned_by_current,
    _policy_for,
    missing_policies,
)
from django_tenant_domains.sharing import READ, WRITE
from tests.support import BYPASS, POSTGRES, leak_db
from tests.testapp.models import ArticleTenantLink, CodeTenantLink, CustomChild, Review, Tag
from tests.thirdparty.models import Article, ArticleProxy, Author, Book, Code, Snippet

pytestmark = leak_db
on_postgres = pytest.mark.skipif(not POSTGRES, reason="needs row-level security")


@pytest.fixture
def acme():
    return Tenant.objects.create(slug="acme", name="Acme")


@pytest.fixture
def globex():
    return Tenant.objects.create(slug="globex", name="Globex")


@pytest.fixture
def article(acme):
    with tenant_context(acme):
        return Article.objects.create(title="acme")


def links(record):
    return link_of(type(record)).unscoped_objects.using(BYPASS).filter(record_id=record.pk)


def stored(record):
    return type(record)._base_manager.using(BYPASS).get(pk=record.pk)


def reach(tenant, model=Article):
    with tenant_context(tenant):
        return set(external_records(model).values_list("pk", flat=True))


def raw(tenant, sql, params=()):
    with tenant_context(tenant), connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def test_register_generates_a_model_beside_the_caller():
    assert ArticleTenantLink.__name__ == "ArticleTenantLink"
    assert ArticleTenantLink._meta.app_label == "testapp"
    assert link_of(ArticleProxy) is ArticleTenantLink


def test_registering_twice_returns_the_same_model():
    assert register_external(Article) is ArticleTenantLink


def _other():
    class Other:
        pass

    Other._meta = SimpleNamespace(concrete_model=Other)
    return Other


def test_same_generated_name_is_refused():
    scope = {"__name__": "tests.testapp.models", "register": register_external, "other": _other()}
    with pytest.raises(ImproperlyConfigured, match="already has a model"):
        exec("register(other, name='ArticleTenantLink')", scope)  # noqa: S102


def test_registering_outside_an_app_is_refused():
    with pytest.raises(ImproperlyConfigured, match="installed app"):
        register_external(_other())


def test_default_tenant_model_does_not_need_the_setting():
    with override_settings():
        del settings.TENANT_DOMAINS_TENANT_MODEL
        assert external._tenant_model() == "django_tenant_domains.Tenant"


def test_migrations_are_complete():
    call_command("makemigrations", check=True, dry_run=True, verbosity=0)


def test_third_party_table_is_unchanged():
    with connection.cursor() as cursor:
        described = connection.introspection.get_table_description(cursor, Article._meta.db_table)
    assert {column.name for column in described} == {"id", "title"}


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (Article, {"title": "new"}),
        (ArticleProxy, {"title": "new"}),
        (Snippet, {"body": "new"}),
        (Snippet, {"id": uuid.uuid4(), "body": "new"}),
        (Code, {"code": "abc"}),
    ],
)
def test_creating_stamps_the_tenant_and_reads_back(acme, globex, model, values):
    with tenant_context(acme):
        record = model.objects.create(**values)
        assert model.objects.get(pk=record.pk).pk == record.pk
    only = links(record).get()
    assert (only.tenant_id, only.access) == (acme.pk, OWNER)
    assert record.pk in reach(acme, model)
    assert record.pk not in reach(globex, model)


def test_created_key_is_not_reused(acme):
    with tenant_context(acme):
        first = Article.objects.create(title="one")
        second = Article.objects.create(title="two")
    assert first.pk != second.pk
    assert links(second).count() == 1


def test_get_or_create_and_update_or_create(acme):
    with tenant_context(acme):
        record, created = Article.objects.get_or_create(title="x")
        assert created
        assert Article.objects.get_or_create(title="x") == (record, False)
        Article.objects.update_or_create(pk=record.pk, defaults={"title": "y"})
    assert stored(record).title == "y"
    assert links(record).count() == 1


def test_saving_again_adds_no_link(acme, article):
    with tenant_context(acme):
        article.title = "changed"
        article.save()
    assert stored(article).title == "changed"
    assert links(article).count() == 1


def test_creating_a_key_that_exists_fails_in_the_database(acme):
    with tenant_context(acme):
        Code.objects.create(code="abc")
        with pytest.raises(IntegrityError), transaction.atomic():
            Code(code="abc").save(force_insert=True)
    assert CodeTenantLink.unscoped_objects.using(BYPASS).count() == 1


def test_no_tenant_is_refused_without_a_scope():
    with pytest.raises(TenantNotSet):
        Article(title="x").save()


def test_another_tenants_key_is_refused(acme, globex, article):
    refusal = pytest.raises((TenantMismatch, DatabaseError))
    with tenant_context(globex), refusal, transaction.atomic():
        Article(pk=article.pk, title="stolen").save()
    assert stored(article).title == "acme"
    assert links(article).count() == 1


def test_another_tenant_cannot_save_or_delete(acme, globex, article):
    held = stored(article)
    held._state.db = "default"
    held.title = "stolen"
    with tenant_context(globex):
        with pytest.raises(TenantMismatch):
            held.save()
        with pytest.raises(TenantMismatch), transaction.atomic():
            held.delete()
    assert stored(article).title == "acme"


def test_deleting_removes_the_links(acme, globex, article):
    with tenant_context(acme):
        share(article, globex)
        pk = article.pk
        article.delete()
    assert not Article._base_manager.using(BYPASS).filter(pk=pk).exists()
    assert not ArticleTenantLink.unscoped_objects.using(BYPASS).filter(record_id=pk).exists()


def test_queryset_delete_removes_the_links(acme):
    with tenant_context(acme):
        Article.objects.create(title="x")
        external_records(Article).delete()
    assert not Article._base_manager.using(BYPASS).exists()
    assert not ArticleTenantLink.unscoped_objects.using(BYPASS).exists()


def test_proxy_delete_runs_the_hook(acme, globex, article):
    held = ArticleProxy._base_manager.using(BYPASS).get(pk=article.pk)
    held._state.db = "default"
    with tenant_context(globex), pytest.raises(TenantMismatch):
        held.delete()


def test_read_share_reads_but_cannot_write(acme, globex, article):
    with tenant_context(acme):
        share(article, globex, READ)
    assert article.pk in reach(globex)
    with tenant_context(globex):
        found = external_records(Article).get(pk=article.pk)
        found.title = "changed"
        with pytest.raises(TenantMismatch):
            found.save()
        with pytest.raises(TenantMismatch), transaction.atomic():
            found.delete()


def test_write_share_edits_but_cannot_delete(acme, globex, article):
    with tenant_context(acme):
        share(article, globex, WRITE)
    with tenant_context(globex):
        found = external_records(Article, write_only=True).get(pk=article.pk)
        found.title = "edited"
        found.save()
        with pytest.raises(TenantMismatch), transaction.atomic():
            found.delete()
    assert stored(article).title == "edited"


def test_share_changes_access_and_unshare_ends_it(acme, globex, article):
    with tenant_context(acme):
        share(article, globex, READ)
        share(article, globex, WRITE)
    assert links(article).get(tenant_id=globex.pk).access == WRITE
    with tenant_context(acme):
        unshare(article, globex)
    assert article.pk not in reach(globex)
    assert links(article).get().access == OWNER


def test_only_the_owner_can_share(acme, globex, article):
    third = Tenant.objects.create(slug="third", name="Third")
    with tenant_context(acme):
        share(article, globex, WRITE)
    with tenant_context(globex):
        with pytest.raises(TenantMismatch):
            share(article, third)
        with pytest.raises(TenantMismatch):
            unshare(article, globex)


def test_share_refuses_the_owner_and_unknown_access(acme, globex, article):
    with tenant_context(acme):
        with pytest.raises(TenantMismatch):
            share(article, acme)
        with pytest.raises(ValueError, match="Unknown access"):
            share(article, globex, OWNER)


def test_share_uses_the_records_database(acme, globex, article):
    choose = patch.object(external.router, "db_for_write", return_value="default")
    with choose as chosen, tenant_context(acme):
        share(article, globex)
        unshare(article, globex)
    assert all(call.kwargs["instance"] is article for call in chosen.call_args_list)


def test_helper_update_needs_write_access(acme, globex, article):
    with tenant_context(acme):
        share(article, globex, READ)
    with tenant_context(globex):
        assert external_records(Article).filter(pk=article.pk).update(title="x") == 0
    assert stored(article).title == "acme"
    with tenant_context(acme):
        share(article, globex, WRITE)
    with tenant_context(globex):
        assert external_records(Article).filter(pk=article.pk).update(title="x") == 1
    assert stored(article).title == "x"


def test_helper_update_stays_on_the_write_database(acme, article):
    with tenant_context(acme), patch("django.db.router.db_for_read", return_value="replica"):
        assert external_records(Article).filter(pk=article.pk).update(title="x") == 1


def test_helper_update_passes_routing_hints(acme, article):
    choose = patch.object(external.router, "db_for_write", return_value="default")
    with choose as chosen, tenant_context(acme):
        rows = external_records(Article).filter(pk=article.pk)
        rows._hints = {"instance": article}
        rows.update(title="x")
    assert chosen.called
    assert all(call.kwargs == {"instance": article} for call in chosen.call_args_list)


def test_database_default_key_is_not_treated_as_a_value(acme):
    """It is an expression until the insert, and the link needs a real key before that."""
    record = Snippet()
    record.pk = Value(uuid.uuid4())
    assert external._known_key(record) is None
    record.pk = "abc"
    assert external._known_key(record) == "abc"


def test_link_manager_shows_only_this_tenants_links(acme, globex, article):
    other = Tenant.objects.create(slug="other", name="Other")
    with tenant_context(acme):
        share(article, globex)
    with tenant_context(globex):
        assert [link.tenant_id for link in ArticleTenantLink.objects.all()] == [globex.pk]
    with tenant_context(acme):
        assert ArticleTenantLink.objects.count() == 2
    with tenant_context(other):
        assert not ArticleTenantLink.objects.exists()
    with pytest.raises(TenantNotSet):
        ArticleTenantLink.objects.count()


def test_links_cannot_be_written_directly(acme, globex, article):
    with tenant_context(globex):
        forged = ArticleTenantLink(record_id=article.pk, tenant_id=globex.pk, access=WRITE)
        with pytest.raises(TenantMismatch):
            forged.save()
        with pytest.raises(TenantMismatch):
            ArticleTenantLink.objects.bulk_create([forged])
        with pytest.raises(TenantMismatch):
            ArticleTenantLink.objects.update(access=WRITE)
        with pytest.raises(TenantMismatch):
            ArticleTenantLink.objects.all().delete()


def test_row_from_before_registration_is_hidden_until_assigned(acme, globex):
    # A row written without the hook, as a data load through the bypass login would be.
    with no_tenant():
        legacy = Article._base_manager.using(BYPASS).bulk_create([Article(title="legacy")])[0]
    assert legacy.pk not in reach(acme)
    with no_tenant():
        assign_owner(legacy, acme, using=BYPASS)
        with pytest.raises(TenantMismatch):
            assign_owner(legacy, globex, using=BYPASS)
    assert legacy.pk in reach(acme)
    assert legacy.pk not in reach(globex)


def test_assign_owner_promotes_a_grantee(acme, globex, article):
    with tenant_context(acme):
        share(article, globex, READ)
    links(article).filter(tenant_id=acme.pk).delete()
    with no_tenant():
        assign_owner(article, globex, using=BYPASS)
    assert links(article).get().access == OWNER
    assert links(article).get().tenant_id == globex.pk


def test_assign_owner_needs_no_tenant(acme, globex, article):
    with tenant_context(globex), pytest.raises(TenantMismatch):
        assign_owner(article, globex)


def test_multi_table_inheritance_is_an_error(monkeypatch):
    class Fake:
        __name__ = "Fake"
        _meta = SimpleNamespace(get_parent_list=lambda: [object()])

    monkeypatch.setattr(checks, "external_models", lambda: [Fake])
    monkeypatch.setattr(checks, "has_sharing", lambda model: False)
    assert [e.id for e in checks.check_external_models(None)] == ["tenant_domains.E011"]


def test_scoped_model_is_an_error(monkeypatch):
    class Fake:
        __name__ = "Fake"
        _is_tenant_scoped = True
        _meta = SimpleNamespace(get_parent_list=list)

    monkeypatch.setattr(checks, "external_models", lambda: [Fake])
    monkeypatch.setattr(checks, "has_sharing", lambda model: False)
    monkeypatch.setattr(checks.apps, "get_models", list)
    assert [e.id for e in checks.check_external_models(None)] == ["tenant_domains.E012"]


class TestForeignKeys:
    """A key to a record the tenant cannot see would let it block that record's deletion."""

    @pytest.fixture
    def tag(self, acme):
        with tenant_context(acme):
            return Tag.objects.create(name="acme")

    @pytest.mark.parametrize("field", ["tag", "article"])
    def test_key_to_another_tenants_record_is_refused(self, globex, tag, article, field):
        target = {"tag": tag, "article": article}[field]
        with tenant_context(globex), pytest.raises(TenantMismatch):
            Review.objects.create(**{field: target})
        assert not Review._base_manager.using(BYPASS).exists()

    def test_changing_a_key_to_another_tenants_record_is_refused(self, acme, globex, article):
        with tenant_context(globex):
            theirs = Article.objects.create(title="globex")
            review = Review.objects.create(article=theirs)
            review.article_id = article.pk
            with pytest.raises(TenantMismatch):
                review.save()
        with tenant_context(acme):
            Article.objects.get(pk=article.pk).delete()
        assert not Article._base_manager.using(BYPASS).filter(pk=article.pk).exists()

    def test_own_and_shared_targets_are_accepted(self, acme, globex, tag, article):
        with tenant_context(acme):
            share(article, globex, READ)
            Review.objects.create(tag=tag, article=article)
        with tenant_context(globex):
            Review.objects.create(article=article)
        assert Review._base_manager.using(BYPASS).count() == 2

    @pytest.mark.parametrize("field", ["tag", "article"])
    def test_write_grantee_cannot_point_the_owners_record_at_its_own(self, acme, globex, field):
        with tenant_context(acme):
            review = Review.objects.create()
            share(review, globex, WRITE)
        with tenant_context(globex):
            mine = Tag.objects.create(name="globex") if field == "tag" else Article.objects.create()
            found = external_records(Review, write_only=True).get(pk=review.pk)
            setattr(found, field, mine)
            with pytest.raises(TenantMismatch), transaction.atomic():
                found.save()
        stored = Review._base_manager.using(BYPASS).get(pk=review.pk)
        assert getattr(stored, f"{field}_id") is None

    def test_write_grantee_can_save_with_keys_it_cannot_see(self, acme, globex, tag, article):
        with tenant_context(acme):
            review = Review.objects.create(tag=tag, article=article)
            share(review, globex, WRITE)
        with tenant_context(globex):
            found = external_records(Review, write_only=True).get(pk=review.pk)
            found.save()
            found.tag = None
            found.save()
        assert stored(review).tag_id is None


class TestObjectPermissions:
    """Third-party records have no tenant column, so their links decide."""

    PERMISSIONS = ["testapp.view_article", "testapp.change_article", "testapp.publish_article"]

    @pytest.fixture
    def member(self, globex):
        role = Role.objects.create(tenant=globex, name="editor", permissions=self.PERMISSIONS)
        user = get_user_model().objects.create_user("member")
        Membership.objects.create(user=user, tenant=globex, role=role)
        return get_user_model().objects.get(pk=user.pk)

    def test_unshared_record_is_refused(self, globex, member, article):
        with tenant_context(globex):
            for perm in self.PERMISSIONS:
                assert member.has_perm(perm, article) is False
            assert member.get_all_permissions(article) == set()

    @pytest.mark.parametrize(
        "access,verb,expected",
        [
            (READ, "view", True),
            (READ, "change", False),
            (READ, "publish", False),
            (WRITE, "view", True),
            (WRITE, "change", True),
            (WRITE, "publish", False),
        ],
    )
    def test_grantee_follows_the_share(self, acme, globex, member, article, access, verb, expected):
        with tenant_context(acme):
            share(article, globex, access)
        with tenant_context(globex):
            assert member.has_perm(f"testapp.{verb}_article", article) is expected

    def test_owner_gets_the_roles_answer(self, globex, member):
        with tenant_context(globex):
            own = Article.objects.create(title="globex")
            assert all(member.has_perm(perm, own) for perm in self.PERMISSIONS)
            assert member.get_all_permissions(own) == set(self.PERMISSIONS)

    def test_with_perm_follows_the_links(self, acme, globex, member, article):
        backend = "django_tenant_domains.backends.TenantBackend"
        users = get_user_model().objects
        with tenant_context(globex):
            assert member not in users.with_perm(
                "testapp.view_article", backend=backend, obj=article
            )
        with tenant_context(acme):
            share(article, globex, READ)
        with tenant_context(globex):
            assert member in users.with_perm("testapp.view_article", backend=backend, obj=article)

    def test_restricted_superuser_follows_the_links(self, globex, article):
        Tenant.objects.filter(pk=globex.pk).update(restrict_superusers=True)
        globex.refresh_from_db()
        role = Role.objects.create(tenant=globex, name="editor", permissions=self.PERMISSIONS)
        root = get_user_model().objects.create_superuser("root")
        Membership.objects.create(user=root, tenant=globex, role=role)
        shield_restricted_superuser(root)
        with tenant_context(globex):
            assert root.has_perm("testapp.view_article", article) is False


def test_registered_models_pass_the_checks():
    assert checks.check_external_models(None) == []


@on_postgres
class TestRowLevelSecurity:
    def test_every_table_is_protected(self):
        assert missing_policies() == []
        with connections["migrator"].cursor() as cursor:
            cursor.execute(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)",
                [Article._meta.db_table],
            )
            assert cursor.fetchone()[0] is True
            cursor.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = to_regclass(%s) "
                "AND NOT tgisinternal",
                [Article._meta.db_table],
            )
            assert cursor.fetchone()[0] == 0

    def test_third_party_manager_is_bound_by_the_database(self, acme, globex, article):
        with tenant_context(globex):
            assert not Article.objects.filter(pk=article.pk).exists()
            assert Article.objects.filter(pk=article.pk).update(title="x") == 0
            assert Article.objects.filter(pk=article.pk).delete()[0] == 0
        with tenant_context(acme):
            assert Article.objects.filter(pk=article.pk).exists()
        assert stored(article).title == "acme"

    def test_no_tenant_reads_nothing(self, article):
        with no_tenant(), transaction.atomic():
            assert not Article.objects.exists()

    def test_creating_needs_a_transaction(self, acme):
        previous = _switch(_State(tenant=acme))
        try:
            with pytest.raises(TenantMismatch, match="needs a transaction"):
                Article(title="x").save()
        finally:
            _switch(previous)

    def test_table_is_refused_until_its_links_table_exists(self, monkeypatch):
        policy = TablePolicy(
            "thirdparty_article", '"thirdparty_article"', read="x", requires=('"no_such_table"',)
        )
        monkeypatch.setattr("django_tenant_domains.rls.policies.table_policies", lambda c: [policy])
        located = _located(connections["migrator"])
        assert [found.expression for found, _ in located] == ["false"]

    def test_many_to_many_table_waits_for_its_links_table(self):
        policy = _policy_for(Book.authors.through, connections["migrator"])
        assert policy.requires == ('"testapp_booktenantlink"',)

    def test_write_rule_follows_the_parent_link_of_a_separate_key(self):
        rule = _owned_by_current(CustomChild, "child_id", "k", connections["migrator"])
        assert '"testapp_customchild"."child_id" = k' in rule
        project = '"testapp_project"."id" = "testapp_customchild"."project_ptr_id"'
        assert project in rule

    def test_functions_name_their_tables_in_full(self):
        policy = _policy_for(ArticleTenantLink, connections["migrator"])
        assert all("FROM public." in sql for sql in policy.setup)

    def test_pending_grant_holds_the_record_against_deletion(self, acme, globex, article):
        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                other.set_autocommit(False)
                cursor.execute("SELECT set_config(%s, %s, true)", [SETTING_NAME, str(acme.pk)])
                cursor.execute(
                    "INSERT INTO testapp_articletenantlink "
                    "(record_id, tenant_id, access, created_at) VALUES (%s, %s, 'write', now())",
                    [article.pk, globex.pk],
                )
                with tenant_context(acme), connection.cursor() as mine:
                    mine.execute("SET LOCAL lock_timeout = '200ms'")
                    with pytest.raises(DatabaseError, match="lock timeout"), transaction.atomic():
                        article.delete()
                other.rollback()
        finally:
            other.close()
        assert stored(article)

    def test_reserving_refuses_a_key_with_no_sequence(self):
        with pytest.raises(TenantMismatch, match="auto-generated"):
            external._reserve_key(Code, "default")

    def test_many_to_many_rows_follow_the_record(self, acme, globex):
        author = Author._base_manager.using(BYPASS).create(name="a")
        through = Book.authors.through
        with tenant_context(acme):
            book = Book.objects.create(title="b")
            book.authors.add(author.pk)
        table = through._meta.db_table
        assert raw(globex, f"DELETE FROM {table}") == 0
        with tenant_context(globex):
            assert not through.objects.filter(book_id=book.pk).exists()
        with pytest.raises(DatabaseError):
            raw(
                globex,
                f"INSERT INTO {table} (book_id, author_id) VALUES (%s, %s)",
                [book.pk, author.pk],
            )
        with tenant_context(acme):
            share(book, globex, READ)
        with tenant_context(globex):
            assert through.objects.filter(book_id=book.pk).exists()
        assert raw(globex, f"DELETE FROM {table}") == 0
        with tenant_context(acme):
            share(book, globex, WRITE)
        assert raw(globex, f"DELETE FROM {table}") == 1
        assert missing_policies() == []

    def test_failed_insert_leaves_no_link(self, acme):
        with tenant_context(acme), pytest.raises(DatabaseError), transaction.atomic():
            Article(title="x" * 100).save()
        assert not ArticleTenantLink.unscoped_objects.using(BYPASS).exists()

    def test_bulk_create_is_refused(self, acme):
        with tenant_context(acme), pytest.raises(DatabaseError), transaction.atomic():
            Article.objects.bulk_create([Article(title="x")])

    def test_raw_insert_without_a_link_is_refused(self, acme):
        with pytest.raises(DatabaseError):
            raw(acme, "INSERT INTO thirdparty_article (title) VALUES ('x')")
        assert not Article._base_manager.using(BYPASS).exists()

    @pytest.mark.parametrize("access", [OWNER, WRITE, READ])
    def test_tenant_cannot_link_itself_to_another_tenants_record(
        self, acme, globex, article, access
    ):
        with pytest.raises(DatabaseError):
            raw(
                globex,
                "INSERT INTO testapp_articletenantlink "
                "(record_id, tenant_id, access, created_at) VALUES (%s, %s, %s, now())",
                [article.pk, globex.pk, access],
            )
        assert article.pk not in reach(globex)

    def test_tenant_cannot_claim_a_row_with_no_owner(self, globex):
        legacy = Article._base_manager.using(BYPASS).bulk_create([Article(title="legacy")])[0]
        with pytest.raises(DatabaseError):
            raw(
                globex,
                "INSERT INTO testapp_articletenantlink "
                "(record_id, tenant_id, access, created_at) VALUES (%s, %s, 'owner', now())",
                [legacy.pk, globex.pk],
            )

    def test_link_to_a_record_that_never_appears_fails_at_commit(self, globex):
        with pytest.raises(DatabaseError):
            raw(
                globex,
                "INSERT INTO testapp_articletenantlink "
                "(record_id, tenant_id, access, created_at) VALUES (999999, %s, 'owner', now())",
                [globex.pk],
            )

    def test_grantee_cannot_raise_its_own_access(self, acme, globex, article):
        with tenant_context(acme):
            share(article, globex, READ)
        for level in (WRITE, OWNER):
            assert (
                raw(
                    globex,
                    "UPDATE testapp_articletenantlink SET access = %s WHERE record_id = %s",
                    [level, article.pk],
                )
                == 0
            )
        assert links(article).get(tenant_id=globex.pk).access == READ

    def test_grantee_cannot_end_or_create_shares(self, acme, globex, article):
        third = Tenant.objects.create(slug="third", name="Third")
        with tenant_context(acme):
            share(article, globex, READ)
        assert (
            raw(globex, "DELETE FROM testapp_articletenantlink WHERE record_id = %s", [article.pk])
            == 0
        )
        with pytest.raises(DatabaseError):
            raw(
                globex,
                "INSERT INTO testapp_articletenantlink "
                "(record_id, tenant_id, access, created_at) VALUES (%s, %s, 'read', now())",
                [article.pk, third.pk],
            )

    def test_owner_cannot_strand_its_record(self, acme, article):
        sql = "DELETE FROM testapp_articletenantlink WHERE record_id = %s"
        assert raw(acme, sql, [article.pk]) == 0
        assert links(article).count() == 1

    def test_owner_cannot_move_a_link_or_demote_itself(self, acme, globex, article):
        with tenant_context(acme):
            share(article, globex, READ)
        with pytest.raises(DatabaseError):
            raw(
                acme,
                "UPDATE testapp_articletenantlink SET tenant_id = %s "
                "WHERE record_id = %s AND access = 'read'",
                [acme.pk, article.pk],
            )
        sql = "UPDATE testapp_articletenantlink SET access = 'read' WHERE access = 'owner'"
        assert raw(acme, sql) == 0

    def test_owner_cannot_raw_delete_a_record_with_links(self, acme, article):
        with pytest.raises(DatabaseError):
            raw(acme, "DELETE FROM thirdparty_article WHERE id = %s", [article.pk])
        assert stored(article)

    def test_read_grantee_cannot_write_through_sql(self, acme, globex, article):
        with tenant_context(acme):
            share(article, globex, READ)
        assert raw(globex, "UPDATE thirdparty_article SET title = 'x'") == 0
        assert raw(globex, "DELETE FROM thirdparty_article") == 0

    def test_write_grantee_can_edit_but_not_delete_through_sql(self, acme, globex, article):
        with tenant_context(acme):
            share(article, globex, WRITE)
        assert raw(globex, "UPDATE thirdparty_article SET title = 'x'") == 1
        assert raw(globex, "DELETE FROM thirdparty_article") == 0

    def test_grantee_cannot_change_a_records_key(self, acme, globex, article):
        with tenant_context(acme):
            share(article, globex, WRITE)
        with pytest.raises(DatabaseError):
            raw(globex, "UPDATE thirdparty_article SET id = id + 1000")

    def test_setting_is_not_left_behind(self, acme):
        with tenant_context(acme):
            Article.objects.create(title="x")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting(%s, true)", [SETTING_NAME])
            assert not cursor.fetchone()[0]
