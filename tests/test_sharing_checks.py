from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.test import override_settings

from django_tenant_domains import checks, rls, sharing
from django_tenant_domains.models import TenantModel
from django_tenant_domains.rls import policies
from django_tenant_domains.rls.policies import TablePolicy
from django_tenant_domains.sharing import SharingConfig
from tests.testapp.models import Binder, Folder, FolderProxy, KeyedParent, Page, Project


def test_bad_cascade_name_is_an_error(monkeypatch):
    fake = SimpleNamespace(
        _tenant_sharing=SharingConfig(None, ("nope",), False),
        _meta=Folder._meta,
        __name__="Fake",
    )
    monkeypatch.setattr(checks, "shareable_models", lambda: [fake])
    monkeypatch.setattr(checks, "has_sharing", lambda model: False)
    ids = [error.id for error in checks.check_shareable_models(None)]
    assert ids == ["tenant_domains.E005"]


def test_sharing_with_multi_table_inheritance_is_an_error(monkeypatch):
    monkeypatch.setattr(checks, "shareable_models", list)
    monkeypatch.setattr(checks, "has_sharing", lambda model: model is Project)
    with override_settings():
        ids = [error.id for error in checks.check_shareable_models(None)]
    assert ids == ["tenant_domains.E006"]


def test_cascade_back_to_itself_is_an_error():
    assert checks._reaches("a", "a", {"a": ["a"]})
    assert checks._reaches("a", "a", {"a": ["b"], "b": ["a"]})
    assert not checks._reaches("a", "a", {"a": ["b"], "b": ["c"]})


def test_cascade_over_an_alternate_key_is_an_error(monkeypatch):
    class Fake:
        _tenant_sharing = SharingConfig(None, ("children",), False)
        _meta = KeyedParent._meta

    monkeypatch.setattr(checks, "shareable_models", lambda: [Fake])
    monkeypatch.setattr(checks, "has_sharing", lambda model: False)
    ids = [error.id for error in checks.check_shareable_models(None)]
    assert "tenant_domains.E007" in ids


def test_invalid_cascade_is_reported_rather_than_crashing(monkeypatch):
    class Fake:
        _tenant_sharing = SharingConfig(None, ("missing",), False)
        _meta = KeyedParent._meta

    monkeypatch.setattr(sharing, "shareable_models", lambda: [Fake])
    monkeypatch.setattr(checks, "shareable_models", lambda: [Fake])
    ids = [error.id for error in checks.check_shareable_models(None)]
    assert "tenant_domains.E005" in ids


def test_long_model_names_get_short_index_names():
    long_name = "SharedDocumentWithAVeryLongModelNameIndeed"
    model = type(
        long_name,
        (TenantModel,),
        {"__module__": "tests.testapp.models", "Meta": type("Meta", (), {"app_label": "testapp"})},
    )
    try:
        share_model = sharing._make_share_model(model)
        assert all(len(index.name) <= 30 for index in share_model._meta.indexes)
    finally:
        for name in (long_name.lower(), f"{long_name.lower()}share"):
            apps.all_models["testapp"].pop(name, None)
        apps.clear_cache()


def test_trigger_arguments_escape_apostrophes():
    policy = TablePolicy(
        "t",
        '"t"',
        read="true",
        insert="true",
        update_using="true",
        update_check="true",
        delete="true",
        keep_columns=("record'key",),
    )
    connection = SimpleNamespace()
    with patch.object(policies, "_located", return_value=[(policy, 1)]):
        statements = policies.policy_sql(connection)
    assert any("'record''key'" in statement for statement in statements)


def test_braces_in_a_table_name_do_not_break_policy_generation():
    policy = TablePolicy(
        "t",
        '"a{b}"',
        read="true",
        insert="true",
        update_using="true",
        update_check="true",
        delete="true",
    )
    assert all('"a{b}"' in statement for statement in policies._split_policies(policy))


def test_proxy_of_a_shared_model_is_not_multi_table_inheritance():
    ids = [error.id for error in checks.check_shareable_models(None)]
    assert "tenant_domains.E006" not in ids


def test_generic_relation_in_a_cascade_is_reported():
    relation = SimpleNamespace(
        one_to_many=True, auto_created=False, concrete=False, related_model=Folder
    )
    assert not sharing.is_reverse_foreign_key(relation)
    assert sharing.is_reverse_foreign_key(Folder._meta.get_field("files"))


def test_chained_cascade_is_an_error(monkeypatch):
    class Top:
        _tenant_sharing = SharingConfig(None, ("pages",), False)
        _meta = Binder._meta

    # Page's own config is what the real check would find as a child that also cascades.
    monkeypatch.setattr(Page, "_tenant_sharing", SharingConfig(None, ("notes",), False))
    monkeypatch.setattr(checks, "shareable_models", lambda: [Top, Page])
    monkeypatch.setattr(checks, "has_sharing", lambda model: False)
    ids = [error.id for error in checks.check_shareable_models(None)]
    assert "tenant_domains.E009" in ids


def _link_check_ids(monkeypatch, vendor, **rls_setting):
    databases = {"default": SimpleNamespace(vendor=vendor)}
    monkeypatch.setattr(checks, "shareable_models", lambda: [Page])
    monkeypatch.setattr(checks, "connections", databases)
    monkeypatch.setattr(rls, "connections", databases)
    with override_settings(**rls_setting):
        return [error.id for error in checks.check_shareable_models(None)]


def test_links_on_postgres_without_row_level_security_are_an_error(monkeypatch):
    ids = _link_check_ids(monkeypatch, "postgresql", TENANT_DOMAINS_RLS=False)
    assert ids == ["tenant_domains.E010"]


def test_links_on_sqlite_only_warn(monkeypatch):
    assert _link_check_ids(monkeypatch, "sqlite") == ["tenant_domains.W004"]


def test_links_on_postgres_with_row_level_security_pass(monkeypatch):
    assert _link_check_ids(monkeypatch, "postgresql", TENANT_DOMAINS_RLS=True) == []


def test_any_postgres_database_makes_links_without_row_level_security_an_error(monkeypatch):
    databases = {
        "default": SimpleNamespace(vendor="sqlite"),
        "reports": SimpleNamespace(vendor="postgresql"),
    }
    monkeypatch.setattr(checks, "shareable_models", lambda: [Page])
    monkeypatch.setattr(checks, "connections", databases)
    with override_settings(TENANT_DOMAINS_RLS=False):
        ids = [error.id for error in checks.check_shareable_models(None)]
    assert ids == ["tenant_domains.E010"]


def test_sharing_without_links_needs_no_row_level_security(monkeypatch):
    databases = {"default": SimpleNamespace(vendor="postgresql")}
    monkeypatch.setattr(checks, "shareable_models", lambda: [Binder])
    monkeypatch.setattr(checks, "connections", databases)
    with override_settings(TENANT_DOMAINS_RLS=False):
        assert checks.check_shareable_models(None) == []


def test_links_only_warn_when_postgres_is_just_the_bypass_login(monkeypatch):
    databases = {
        "default": SimpleNamespace(vendor="sqlite"),
        "migrator": SimpleNamespace(vendor="postgresql"),
    }
    monkeypatch.setattr(checks, "shareable_models", lambda: [Page])
    monkeypatch.setattr(checks, "connections", databases)
    monkeypatch.setattr(rls, "connections", databases)
    with override_settings(TENANT_DOMAINS_RLS=True):
        ids = [error.id for error in checks.check_shareable_models(None)]
    assert ids == ["tenant_domains.W004"]


def test_sharing_on_a_proxy_is_an_error(monkeypatch):
    class Fake:
        _tenant_sharing = SharingConfig(None, (), False)
        _meta = FolderProxy._meta

    monkeypatch.setattr(checks, "shareable_models", lambda: [Fake])
    ids = [error.id for error in checks.check_shareable_models(None)]
    assert ids == ["tenant_domains.E014"]


def test_proxy_of_a_shared_model_is_not_an_error():
    assert FolderProxy in sharing.shareable_models()
    ids = [error.id for error in checks.check_shareable_models(None)]
    assert "tenant_domains.E014" not in ids
