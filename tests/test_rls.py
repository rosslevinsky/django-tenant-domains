"""Row-level security, without a wrapping test transaction. Postgres only."""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.apps import apps
from django.core.checks import run_checks
from django.core.management import call_command
from django.db import DatabaseError, connection, connections, models, transaction
from django.db.utils import ConnectionHandler
from django.test import override_settings

from django_tenant_domains import checks, rls
from django_tenant_domains.checks import check_protected_logins_and_tables, check_shareable_models
from django_tenant_domains.context import get_current_tenant, no_tenant, tenant_context
from django_tenant_domains.models import Tenant, TenantModel
from django_tenant_domains.rls import policies
from django_tenant_domains.rls.policies import (
    _owned_by_current,
    missing_policies,
    policy_sql,
    sync_policies,
    table_policies,
)
from django_tenant_domains.sharing import WRITE, config_of, share, shareable
from tests.support import ALL_DATABASES, peek
from tests.testapp.models import (
    Binder,
    CodedChild,
    CodedParent,
    Folder,
    FolderProxy,
    Page,
    Project,
    SpecialProject,
    Tag,
    Task,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.django_db(transaction=True, databases=ALL_DATABASES),
]


@pytest.fixture
def two():
    a = Tenant.objects.create(slug="a", name="A")
    b = Tenant.objects.create(slug="b", name="B")
    for tenant in (a, b):
        with tenant_context(tenant):
            project = Project.objects.create(name=f"{tenant.slug}-project")
            Task.objects.create(title=f"{tenant.slug}-task", project=project)
            Tag.objects.create(name=f"{tenant.slug}-tag")
    return a, b


def raw_names(table="testapp_project"):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT name FROM {table}")
        return {row[0] for row in cursor.fetchall()}


def setting():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_tenant', true)")
        return cursor.fetchone()[0]


def test_tests_are_not_inside_a_transaction():
    assert not connection.in_atomic_block


def test_raw_sql_with_no_tenant_returns_no_rows(two):
    assert raw_names() == set()


def test_raw_sql_under_a_tenant_returns_only_its_rows(two):
    with tenant_context(two[0]):
        assert raw_names() == {"a-project"}


def test_bypass_login_sees_everything(two):
    assert peek(Project).count() == 2


def test_tables_are_owned_by_the_bypass_login():
    with connection.cursor() as cursor:
        cursor.execute("SELECT tableowner FROM pg_tables WHERE tablename = 'testapp_project'")
        assert cursor.fetchone()[0] == "dtd_migrator"


def test_row_level_security_is_forced_so_it_binds_a_table_owner_too():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'testapp_tag'"
        )
        assert cursor.fetchone() == (True, True)


def test_raw_insert_for_another_tenant_is_refused(two):
    a, b = two
    with tenant_context(a), pytest.raises(DatabaseError), connection.cursor() as cursor:
        cursor.execute("INSERT INTO testapp_tag (name, tenant_id) VALUES ('planted', %s)", [b.pk])
    assert not peek(Tag).filter(name="planted").exists()


def test_raw_update_cannot_reach_another_tenant(two):
    a, _ = two
    with tenant_context(a), connection.cursor() as cursor:
        cursor.execute("UPDATE testapp_project SET name = 'changed'")
        assert cursor.rowcount == 1
    assert peek(Project).filter(name="b-project").exists()


def test_raw_delete_cannot_reach_another_tenant(two):
    a, _ = two
    with tenant_context(a), connection.cursor() as cursor:
        cursor.execute("DELETE FROM testapp_tag")
        assert cursor.rowcount == 1
    assert peek(Tag).filter(name="b-tag").exists()


def test_writes_outside_any_tenant_are_refused_by_the_database(two):
    with pytest.raises(DatabaseError), connection.cursor() as cursor:
        cursor.execute("INSERT INTO testapp_tag (name, tenant_id) VALUES ('x', %s)", [two[0].pk])


def test_tenant_context_commits_outside_a_request(two):
    with tenant_context(two[0]):
        Tag.objects.create(name="kept")
    assert peek(Tag).filter(name="kept").exists()


def test_tenant_context_rolls_back_on_error(two):
    with pytest.raises(ValueError), tenant_context(two[0]):
        Tag.objects.create(name="gone")
        raise ValueError
    assert not peek(Tag).filter(name="gone").exists()


def test_setting_is_gone_after_the_context(two):
    with tenant_context(two[0]):
        assert setting() == str(two[0].pk)
    assert not setting()


def test_nested_contexts_keep_the_database_in_step(two):
    a, b = two
    with tenant_context(a):
        with tenant_context(b):
            assert setting() == str(b.pk)
            assert raw_names() == {"b-project"}
        assert setting() == str(a.pk)
        assert get_current_tenant() == a
        assert raw_names() == {"a-project"}


def test_nested_context_error_restores_the_outer_tenant(two):
    a, b = two
    with tenant_context(a):
        with pytest.raises(ValueError), tenant_context(b):
            raise ValueError
        assert setting() == str(a.pk)
        assert raw_names() == {"a-project"}


def test_no_tenant_clears_the_setting_and_restores_it(two):
    with tenant_context(two[0]):
        with no_tenant():
            assert not setting()
            assert raw_names() == set()
        assert setting() == str(two[0].pk)


def test_no_tenant_alone_lifts_only_the_orm_filter(two):
    with no_tenant():
        assert Project.objects.count() == 0


def test_no_tenant_with_the_bypass_login_sees_every_tenant(two):
    with no_tenant():
        assert Project.objects.using("migrator").count() == 2


def test_setting_does_not_outlive_a_committed_transaction(two):
    with tenant_context(two[0]):
        pass
    with transaction.atomic():
        assert raw_names() == set()


def test_inherited_child_table_is_protected(two):
    a, b = two
    with tenant_context(a):
        SpecialProject.objects.create(name="special", level=1)
    with tenant_context(b):
        assert raw_names("testapp_specialproject JOIN testapp_project ON id = project_ptr_id") == (
            set()
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM testapp_specialproject")
            assert cursor.fetchone()[0] == 0


def test_many_to_many_table_is_protected(two):
    a, b = two
    with tenant_context(a):
        Project.objects.get().tags.add(Tag.objects.get())
    with tenant_context(b), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM testapp_project_tags")
        assert cursor.fetchone()[0] == 0


def test_raw_many_to_many_row_across_tenants_is_refused(two):
    a, _ = two
    their_tag = peek(Tag).get(name="b-tag")
    with tenant_context(a):
        project = Project.objects.get()
        with pytest.raises(DatabaseError), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO testapp_project_tags (project_id, tag_id) VALUES (%s, %s)",
                [project.pk, their_tag.pk],
            )


def test_forged_instance_refresh_finds_nothing(two):
    a, _ = two
    theirs = peek(Project).get(name="b-project")
    with tenant_context(a), pytest.raises(Project.DoesNotExist):
        Project(pk=theirs.pk).refresh_from_db()


def test_foreign_key_to_a_hidden_row_is_refused(two):
    from django_tenant_domains.exceptions import TenantMismatch

    a, _ = two
    theirs = peek(Project).get(name="b-project")
    with tenant_context(a), pytest.raises(TenantMismatch):
        Task.objects.create(title="x", project_id=theirs.pk)


def test_every_scoped_table_has_a_policy():
    assert missing_policies() == []


def test_check_fails_when_a_table_loses_its_policy():
    with connections["migrator"].cursor() as cursor:
        cursor.execute("DROP POLICY tenant_domains_isolation ON testapp_tag")
    try:
        assert missing_policies() == ["testapp_tag"]
        with pytest.raises(Exception, match="testapp_tag"):
            call_command("tenant_rls", check=True)
        errors = run_checks(tags=["database"], databases=["default"])
        assert "tenant_domains.E003" in [error.id for error in errors]
    finally:
        call_command("tenant_rls")
    assert missing_policies() == []


def test_check_fails_when_row_level_security_is_not_forced():
    with connections["migrator"].cursor() as cursor:
        cursor.execute("ALTER TABLE testapp_tag NO FORCE ROW LEVEL SECURITY")
    try:
        assert missing_policies() == ["testapp_tag"]
    finally:
        call_command("tenant_rls")


def test_migrate_restores_a_missing_policy():
    with connections["migrator"].cursor() as cursor:
        cursor.execute("DROP POLICY tenant_domains_isolation ON testapp_tag")
    call_command("migrate", database="migrator", verbosity=0)
    assert missing_policies() == []


def test_database_check_passes():
    errors = run_checks(tags=["database"], databases=["default"])
    assert [error.id for error in errors if error.id.startswith("tenant_domains")] == []


def _route_checks_to(monkeypatch, databases):
    for module in (checks, rls, policies):
        monkeypatch.setattr(module, "connections", databases)


def test_check_fails_when_every_postgres_alias_is_the_bypass_login(monkeypatch):
    # The application's only alias uses the bypass login, so nothing is protected.
    _route_checks_to(monkeypatch, {"default": connections["migrator"]})
    with override_settings(TENANT_DOMAINS_RLS_BYPASS_DATABASE="default"):
        assert rls.protected_aliases() == []
        ids = [issue.id for issue in run_checks(databases=["default"])]
    assert "tenant_domains.E013" in ids


def test_check_fails_when_the_bypass_login_is_bound_by_row_level_security(monkeypatch):
    app = connections["default"]
    _route_checks_to(monkeypatch, {"default": app, "migrator": app})
    ids = [issue.id for issue in check_protected_logins_and_tables(None, databases=["default"])]
    assert ids == ["tenant_domains.E015"]


def test_check_finds_missing_policies_in_each_protected_database(monkeypatch):
    name = "dtd_analytics_" + uuid4().hex
    owner = connections["migrator"]
    with owner.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')
    handler = ConnectionHandler(
        {
            "default": {**owner.settings_dict, "NAME": name},
            "analytics": {**connections["default"].settings_dict, "NAME": name},
        }
    )
    try:
        with handler["default"].cursor() as cursor:
            cursor.execute("CREATE TABLE testapp_tag (id bigint PRIMARY KEY, tenant_id bigint)")
            cursor.execute("GRANT SELECT ON testapp_tag TO dtd_app")
        _route_checks_to(
            monkeypatch,
            {
                "default": connections["default"],
                "migrator": owner,
                "analytics": handler["analytics"],
            },
        )
        assert "analytics" in rls.protected_aliases()
        errors = check_protected_logins_and_tables(None, databases=["default", "analytics"])
        assert [error.id for error in errors] == ["tenant_domains.E003"]
        assert "'analytics'" in errors[0].msg
        assert "testapp_tag" in errors[0].msg
    finally:
        handler.close_all()
        with owner.cursor() as cursor:
            cursor.execute(f'DROP DATABASE "{name}"')


def test_check_inspects_a_database_once_however_many_aliases_reach_it(monkeypatch):
    _route_checks_to(
        monkeypatch,
        {
            "default": connections["default"],
            "migrator": connections["migrator"],
            "again": connections["default"],
        },
    )
    calls = []
    real = policies.missing_policies

    def counted(using=None):
        calls.append(using)
        return real(using)

    monkeypatch.setattr(checks, "missing_policies", counted)
    assert check_protected_logins_and_tables(None, databases=["default", "again"]) == []
    assert calls == ["default"]


def test_sql_option_prints_policies(capsys):
    call_command("tenant_rls", sql=True)
    assert "FORCE ROW LEVEL SECURITY" in capsys.readouterr().out


def test_child_linked_by_another_field_follows_that_field_not_the_parent_key(two):
    a, b = two
    with connections["migrator"].cursor() as cursor:
        # The ids and codes are crossed, so a policy using the wrong column picks the wrong owner.
        cursor.execute(
            "INSERT INTO testapp_codedparent (id, code, tenant_id) VALUES (1, 2, %s), (2, 1, %s)",
            [a.pk, b.pk],
        )
        cursor.execute("INSERT INTO testapp_codedchild (parent_id) VALUES (2)")
    with tenant_context(b), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM testapp_codedchild")
        assert cursor.fetchone()[0] == 0
    with tenant_context(a), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM testapp_codedchild")
        assert cursor.fetchone()[0] == 1


def test_save_finds_the_owner_of_a_parent_linked_by_another_field(two):
    a, b = two
    parent = CodedParent.unscoped_objects.using("migrator").bulk_create(
        [CodedParent(code=5, tenant_id=b.pk)]
    )[0]
    forged = CodedChild(parent_id=parent.code)
    assert forged._stored_tenant_ids("migrator") == {b.pk}
    assert a.pk != b.pk


def test_unmanaged_tenant_table_is_protected_and_checked(two):
    with connections["migrator"].cursor() as cursor:
        cursor.execute(
            "INSERT INTO testapp_legacynote (id, text, tenant_id) VALUES (1, 'x', %s)", [two[0].pk]
        )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM testapp_legacynote")
            assert cursor.fetchone()[0] == 0
        with connections["migrator"].cursor() as cursor:
            cursor.execute("DROP POLICY tenant_domains_isolation ON testapp_legacynote")
        assert missing_policies() == ["testapp_legacynote"]
    finally:
        call_command("tenant_rls")
        with connections["migrator"].cursor() as cursor:
            cursor.execute("DELETE FROM testapp_legacynote")


def test_another_permissive_policy_cannot_widen_access(two):
    with connections["migrator"].cursor() as cursor:
        cursor.execute("CREATE POLICY wide_open ON testapp_project USING (true)")
    try:
        assert raw_names() == set()
        with tenant_context(two[0]):
            assert raw_names() == {"a-project"}
    finally:
        with connections["migrator"].cursor() as cursor:
            cursor.execute("DROP POLICY wide_open ON testapp_project")


def test_check_fails_when_the_isolation_policy_is_only_permissive():
    with connections["migrator"].cursor() as cursor:
        cursor.execute("DROP POLICY tenant_domains_isolation ON testapp_tag")
        cursor.execute("CREATE POLICY tenant_domains_isolation ON testapp_tag USING (true)")
    try:
        assert missing_policies() == ["testapp_tag"]
    finally:
        call_command("tenant_rls")


def test_quoted_table_name_is_still_protected_and_checked(monkeypatch):
    monkeypatch.setattr(Tag._meta, "db_table", '"testapp_tag"')
    assert "testapp_tag" in [policy.table for policy in table_policies(connection)]
    assert "testapp_tag" in " ".join(policy_sql(connection))
    with connections["migrator"].cursor() as cursor:
        cursor.execute("ALTER TABLE testapp_tag NO FORCE ROW LEVEL SECURITY")
    try:
        assert missing_policies() == ["testapp_tag"]
    finally:
        call_command("tenant_rls")


def test_the_application_login_gets_no_automatic_access_to_a_view(two):
    with connections["migrator"].cursor() as cursor:
        cursor.execute("CREATE VIEW every_project AS SELECT name FROM testapp_project")
    try:
        call_command("tenant_rls")
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT name FROM every_project")
    finally:
        with connections["migrator"].cursor() as cursor:
            cursor.execute("DROP VIEW every_project")


def test_setting_is_cleared_inside_a_manual_transaction(two):
    transaction.set_autocommit(False)
    try:
        with tenant_context(two[0]):
            assert raw_names() == {"a-project"}
        assert not setting()
        assert raw_names() == set()
    finally:
        transaction.rollback()
        transaction.set_autocommit(True)


def test_schema_qualified_table_name_is_still_protected_and_checked(monkeypatch):
    monkeypatch.setattr(Tag._meta, "db_table", '"public"."testapp_tag"')
    sql = " ".join(policy_sql(connection))
    assert 'ALTER TABLE "public"."testapp_tag" FORCE ROW LEVEL SECURITY' in sql
    call_command("tenant_rls")  # The SQL now spells the table differently.
    assert missing_policies() == []
    with connections["migrator"].cursor() as cursor:
        cursor.execute("ALTER TABLE testapp_tag NO FORCE ROW LEVEL SECURITY")
    try:
        assert missing_policies() == ["public.testapp_tag"]
    finally:
        monkeypatch.undo()
        call_command("tenant_rls")


def test_on_commit_runs_under_the_context_that_registered_it(two):
    a, b = two
    seen = []

    def record():
        seen.append((get_current_tenant(), raw_names()))

    with tenant_context(a):
        transaction.on_commit(record)
        with tenant_context(b):
            transaction.on_commit(record)
        assert seen == []
    assert seen == [(a, {"a-project"}), (b, {"b-project"})]
    assert get_current_tenant() is None
    assert not setting()


def test_on_commit_does_not_run_after_a_rollback(two):
    seen = []
    with pytest.raises(ValueError), tenant_context(two[0]):
        transaction.on_commit(lambda: seen.append(1))
        raise ValueError
    assert seen == []


def test_on_commit_with_robust_keeps_going_after_a_failure(two):
    seen = []

    def boom():
        raise RuntimeError

    with tenant_context(two[0]):
        transaction.on_commit(boom, robust=True)
        transaction.on_commit(lambda: seen.append(raw_names()))
    assert seen == [{"a-project"}]


def test_on_commit_can_write_as_the_tenant(two):
    with tenant_context(two[0]):
        transaction.on_commit(lambda: Tag.objects.create(name="from-hook"))
    assert peek(Tag).get(name="from-hook").tenant == two[0]


def test_a_missing_share_table_makes_its_shared_table_refuse_every_row():
    owner = Tenant.objects.create(slug="owner", name="Owner")
    stranger = Tenant.objects.create(slug="stranger", name="Stranger")
    with tenant_context(owner):
        Folder.objects.create(name="private")
    share_model = config_of(Folder).share_model
    migrator = connections["migrator"]
    with migrator.schema_editor() as editor:
        editor.delete_model(share_model)
    try:
        sync_policies()
        with tenant_context(stranger), connection.cursor() as cursor:
            cursor.execute("SELECT name FROM testapp_folder")
            assert cursor.fetchall() == []
            cursor.execute("UPDATE testapp_folder SET name = 'changed'")
            assert cursor.rowcount == 0
    finally:
        with migrator.schema_editor() as editor:
            editor.create_model(share_model)
        sync_policies()
        # Restore the test runner's cleanup grant, lost when the table was dropped.
        with migrator.cursor() as cursor:
            cursor.execute(f"GRANT TRUNCATE ON {share_model._meta.db_table} TO dtd_app")


def test_ownership_of_a_proxy_is_its_concrete_model_s():
    assert _owned_by_current(FolderProxy, "id", "1", connection) == _owned_by_current(
        Folder, "id", "1", connection
    )


def test_many_to_many_from_a_shared_model_to_an_inheritance_child_gets_a_policy():
    @shareable()
    class ProjectShelf(TenantModel):
        projects = models.ManyToManyField(SpecialProject)

        class Meta:
            app_label = "testapp"

    share_model = config_of(ProjectShelf).share_model
    migrator = connections["migrator"]
    try:
        assert not check_shareable_models(None)
        with migrator.schema_editor() as editor:
            editor.create_model(ProjectShelf)
            editor.create_model(share_model)
        try:
            sync_policies()
            assert missing_policies() == []
        finally:
            with migrator.schema_editor() as editor:
                editor.delete_model(share_model)
                editor.delete_model(ProjectShelf)
    finally:
        for name in ("projectshelf", "projectshelfshare", "projectshelf_projects"):
            apps.all_models["testapp"].pop(name, None)
        apps.clear_cache()


def test_check_fails_when_a_policy_is_out_of_date_with_the_code(monkeypatch):
    owner = Tenant.objects.create(slug="owner", name="Owner")
    grantee = Tenant.objects.create(slug="grantee", name="Grantee")
    with tenant_context(owner):
        binder = Binder.objects.create(name="Shared binder")
        Page.objects.create(binder=binder)
        share(binder, grantee)
    # The code stops cascading the share, but the database still has the old policy.
    monkeypatch.setattr(Binder, "_tenant_sharing", replace(config_of(Binder), cascade=()))
    try:
        assert "testapp_page" in missing_policies()
        with pytest.raises(Exception, match="testapp_page"):
            call_command("tenant_rls", check=True)
    finally:
        monkeypatch.undo()
    assert missing_policies() == []


def test_check_fails_when_a_policy_is_edited_by_hand():
    with connections["migrator"].cursor() as cursor:
        cursor.execute("ALTER POLICY tenant_domains_isolation ON testapp_tag USING (true)")
    try:
        assert missing_policies() == ["testapp_tag"]
    finally:
        call_command("tenant_rls")
    assert missing_policies() == []


def test_check_fails_when_a_policy_no_longer_applies_to_the_application_login(two):
    a, _ = two
    migrator = connections["migrator"].settings_dict["USER"]
    with connections["migrator"].cursor() as cursor:
        cursor.execute(f'ALTER POLICY tenant_domains_isolation ON testapp_project TO "{migrator}"')
    try:
        with tenant_context(a):
            assert raw_names() == {"a-project", "b-project"}
        assert missing_policies() == ["testapp_project"]
    finally:
        call_command("tenant_rls")
    assert missing_policies() == []


def test_check_fails_when_the_allow_policy_is_missing():
    with connections["migrator"].cursor() as cursor:
        cursor.execute("DROP POLICY tenant_domains_access ON testapp_tag")
    try:
        assert missing_policies() == ["testapp_tag"]
    finally:
        call_command("tenant_rls")
    assert missing_policies() == []


@pytest.mark.parametrize(
    "change",
    [
        "DROP TRIGGER tenant_domains_keep_owner ON testapp_folder",
        "ALTER TABLE testapp_folder DISABLE TRIGGER tenant_domains_keep_owner",
    ],
)
def test_check_fails_when_a_shared_table_loses_its_owner_trigger(change):
    acme = Tenant.objects.create(slug="acme", name="Acme")
    globex = Tenant.objects.create(slug="globex", name="Globex")
    with tenant_context(acme):
        folder = Folder.objects.create(name="acme-folder")
        share(folder, globex, WRITE)
    with connections["migrator"].cursor() as cursor:
        cursor.execute(change)
    try:
        with tenant_context(globex), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE testapp_folder SET tenant_id = %s WHERE id = %s", [globex.pk, folder.pk]
            )
        # Without the trigger, the grantee has taken the record over.
        assert peek(Folder).get(pk=folder.pk).tenant_id == globex.pk
        assert missing_policies() == ["testapp_folder"]
    finally:
        call_command("tenant_rls")
    assert missing_policies() == []
