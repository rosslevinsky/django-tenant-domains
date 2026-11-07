import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.utils.functional import SimpleLazyObject

from django_tenant_domains.context import no_tenant, tenant_context
from django_tenant_domains.models import Membership, Role, Tenant
from django_tenant_domains.permissions import (
    membership_for,
    role_allows,
    role_covers,
    user_has_tenant_perm,
)
from tests.testapp.models import Comment, Project, ShieldedUser

pytestmark = pytest.mark.django_db

CHANGE = "testapp.change_project"


@pytest.fixture
def acme():
    return Tenant.objects.create(slug="acme", name="Acme")


@pytest.fixture
def globex():
    return Tenant.objects.create(slug="globex", name="Globex")


def role(name):
    return Role.objects.get(tenant=None, name=name)


def person(tenant=None, role_name=None, name="ann", **kwargs):
    user = get_user_model().objects.create_user(name, **kwargs)
    if tenant:
        Membership.objects.create(user=user, tenant=tenant, role=role(role_name))
    return get_user_model().objects.get(pk=user.pk)


def grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    return get_user_model().objects.get(pk=user.pk)


@pytest.mark.parametrize(
    ("role_name", "view", "add", "change", "delete"),
    [
        ("owner", True, True, True, True),
        ("admin", True, True, True, True),
        ("member", True, True, True, False),
        ("viewer", True, False, False, False),
    ],
)
def test_built_in_role_table(acme, role_name, view, add, change, delete):
    user = person(acme, role_name)
    with tenant_context(acme):
        assert user.has_perm("testapp.view_project") is view
        assert user.has_perm("testapp.add_project") is add
        assert user.has_perm(CHANGE) is change
        assert user.has_perm("testapp.delete_project") is delete


def test_same_person_has_a_different_role_at_each_tenant(acme, globex):
    user = person(acme, "owner")
    Membership.objects.create(user=user, tenant=globex, role=role("viewer"))
    with tenant_context(acme):
        assert user.has_perm(CHANGE)
    with tenant_context(globex):
        assert not user.has_perm(CHANGE)


def test_global_permission_adds_nothing_inside_a_tenant(acme):
    user = grant(person(acme, "viewer"), "change_project")
    with tenant_context(acme):
        assert not user.has_perm(CHANGE)


def test_group_permission_adds_nothing_inside_a_tenant(acme):
    user = person(acme, "viewer")
    group = Group.objects.create(name="support")
    group.permissions.add(Permission.objects.get(codename="change_project"))
    user.groups.add(group)
    user = get_user_model().objects.get(pk=user.pk)
    with tenant_context(acme):
        assert not user.has_perm(CHANGE)
        assert CHANGE not in user.get_all_permissions()


def test_global_permission_with_no_membership_adds_nothing(acme):
    user = grant(person(), "change_project")
    with tenant_context(acme):
        assert not user.has_perm(CHANGE)


def test_global_permission_counts_outside_a_tenant(acme):
    user = grant(person(acme, "viewer"), "change_project")
    assert user.has_perm(CHANGE)
    user = get_user_model().objects.get(pk=user.pk)
    with no_tenant():
        assert user.has_perm(CHANGE)


def test_role_counts_for_nothing_outside_a_tenant(acme):
    assert not person(acme, "owner").has_perm(CHANGE)


def test_a_later_model_backend_cannot_add_global_permissions_back(acme, settings):
    settings.AUTHENTICATION_BACKENDS = [
        "django_tenant_domains.backends.TenantBackend",
        "django.contrib.auth.backends.ModelBackend",
    ]
    user = grant(person(acme, "viewer"), "change_project")
    with tenant_context(acme):
        assert not user.has_perm(CHANGE)


def test_owner_cannot_get_django_wide_permissions(acme):
    user = person(acme, "owner")
    with tenant_context(acme):
        assert not user.has_perm("auth.change_user")
        assert not user.has_module_perms("auth")


def test_explicit_entry_cannot_grant_a_django_wide_permission(acme):
    custom = Role.objects.create(tenant=acme, name="sneaky", permissions=["auth.change_user"])
    assert not role_allows(custom, "auth.change_user")


def test_deactivated_member_has_no_permissions(acme):
    user = person(acme, "owner")
    Membership.objects.filter(user=user).update(is_active=False)
    with tenant_context(acme):
        assert not user.has_perm(CHANGE)


def test_inactive_user_has_no_permissions(acme):
    user = person(acme, "owner", is_active=False)
    with tenant_context(acme):
        assert not user.has_perm(CHANGE)


def test_custom_role_grants_exactly_what_it_lists(acme):
    custom = Role.objects.create(tenant=acme, name="editor", permissions=[CHANGE])
    user = person()
    Membership.objects.create(user=user, tenant=acme, role=custom)
    with tenant_context(acme):
        assert user.has_perm(CHANGE)
        assert not user.has_perm("testapp.delete_project")


def test_custom_codename_in_an_app_with_tenant_models(acme):
    custom = Role.objects.create(tenant=acme, name="editor", permissions=["testapp.publish"])
    assert role_allows(custom, "testapp.publish")
    assert not role_allows(custom, "testapp.other")
    assert role_allows(role("owner"), "testapp.publish")
    assert not role_allows(role("member"), "testapp.publish")


@pytest.mark.parametrize("codename", ["add_invitation", "change_joinrequest", "view_joinrequest"])
def test_managing_people_needs_more_than_member_wildcards(codename):
    perm = f"django_tenant_domains.{codename}"
    assert role_allows(role("admin"), perm)
    assert not role_allows(role("member"), perm)
    assert not role_allows(role("viewer"), perm)


def test_object_from_another_tenant_is_refused(acme, globex):
    user = person(acme, "owner")
    with tenant_context(acme):
        assert user.has_perm(CHANGE, Project(tenant=acme))
        assert not user.has_perm(CHANGE, Project(tenant=globex))


def test_module_permissions_follow_the_role(acme):
    user = person(acme, "viewer")
    with tenant_context(acme):
        assert user.has_module_perms("testapp")
    other = person(name="bob")
    with tenant_context(acme):
        assert not other.has_module_perms("testapp")


def test_all_permissions_follow_the_role(acme):
    user = person(acme, "viewer")
    with tenant_context(acme):
        perms = user.get_all_permissions()
    assert "testapp.view_project" in perms
    assert CHANGE not in perms
    assert "auth.view_user" not in perms


def test_superuser_can_do_anything(acme):
    user = get_user_model().objects.create_superuser("root")
    with tenant_context(acme):
        assert user.has_perm(CHANGE)


def test_restricted_superuser_answered_by_role_in_the_package_helper(acme):
    Tenant.objects.filter(pk=acme.pk).update(restrict_superusers=True)
    acme.refresh_from_db()
    user = get_user_model().objects.create_superuser("root")
    Membership.objects.create(user=user, tenant=acme, role=role("viewer"))
    with tenant_context(acme):
        assert not user_has_tenant_perm(user, CHANGE)
        assert user_has_tenant_perm(user, "testapp.view_project")
        assert user.has_perm(CHANGE)  # Django answers before asking any backend. Documented.


def test_membership_is_read_once_per_user_object(acme, django_assert_num_queries):
    user = person(acme, "owner")
    with (
        tenant_context(acme),
        django_assert_num_queries(2),
    ):  # The membership, then the permission table.
        user.has_perm(CHANGE)
        user.has_perm("testapp.view_project")


def test_async_permission_checks_follow_the_role(acme):
    user = grant(person(acme, "viewer"), "change_project")

    async def main():
        return (
            await user.ahas_perm(CHANGE),
            await user.ahas_perm("testapp.view_project"),
            await user.ahas_module_perms("auth"),
            CHANGE in await user.aget_all_permissions(),
        )

    with tenant_context(acme):
        assert async_to_sync(main)() == (False, True, False, False)


def test_async_permission_checks_stay_global_outside_a_tenant(acme):
    user = grant(person(acme, "viewer"), "change_project")
    assert async_to_sync(user.ahas_perm)(CHANGE)


def test_all_permissions_for_another_tenants_object_is_empty(acme, globex):
    user = person(acme, "viewer")
    with tenant_context(acme):
        assert "testapp.view_project" in user.get_all_permissions(Project(tenant=acme))
        assert user.get_all_permissions(Project(tenant=globex)) == set()


def test_custom_permission_on_a_global_model_is_not_a_tenant_permission(acme):
    content_type = ContentType.objects.get_for_model(Comment)
    Permission.objects.create(codename="moderate_discussion", name="m", content_type=content_type)
    user = person(acme, "owner")
    with tenant_context(acme):
        assert not user.has_perm("testapp.moderate_discussion")
        assert "testapp.moderate_discussion" not in user.get_all_permissions()


def test_a_custom_name_that_looks_like_a_tenant_model_still_follows_its_real_model(acme):
    content_type = ContentType.objects.get_for_model(Comment)
    Permission.objects.create(codename="audit_project", name="a", content_type=content_type)
    user = person(acme, "owner")
    with tenant_context(acme):
        assert not user.has_perm("testapp.audit_project")
        assert "testapp.audit_project" not in user.get_all_permissions()


def test_a_name_shared_by_a_tenant_and_a_global_model_is_refused(acme):
    for model in (Comment, Project):
        content_type = ContentType.objects.get_for_model(model)
        Permission.objects.create(codename="export_data", name="e", content_type=content_type)
    user = person(acme, "owner")
    with tenant_context(acme):
        assert not user.has_perm("testapp.export_data")


def test_custom_permission_on_a_tenant_model_is_a_tenant_permission(acme):
    content_type = ContentType.objects.get_for_model(Project)
    Permission.objects.create(codename="publish_now", name="p", content_type=content_type)
    user = person(acme, "owner")
    with tenant_context(acme):
        assert user.has_perm("testapp.publish_now")


def test_inactive_restricted_superuser_has_nothing(acme):
    Tenant.objects.filter(pk=acme.pk).update(restrict_superusers=True)
    acme.refresh_from_db()
    user = get_user_model().objects.create_superuser("root", is_active=False)
    Membership.objects.create(user=user, tenant=acme, role=role("owner"))
    with tenant_context(acme):
        assert not user_has_tenant_perm(user, CHANGE)


def test_membership_cache_is_shared_through_a_lazy_user(acme, django_assert_num_queries):
    real = person(acme, "owner")
    lazy = SimpleLazyObject(lambda: real)
    with django_assert_num_queries(1):
        membership_for(lazy, acme)
        membership_for(real, acme)


def test_role_cover_rule():
    assert role_covers(role("owner"), role("admin"))
    assert role_covers(role("member"), role("viewer"))
    assert not role_covers(role("member"), role("admin"))
    assert not role_covers(role("viewer"), role("member"))


def test_with_perm_inside_a_tenant_follows_roles(acme, globex):
    member = person(acme, "member", name="m")
    viewer = person(acme, "viewer", name="v")
    outsider = grant(person(globex, "owner", name="o"), "change_project")
    deactivated = person(name="d")
    Membership.objects.create(user=deactivated, tenant=acme, role=role("owner"), is_active=False)
    gone = person(acme, "owner", name="g", is_active=False)
    root = get_user_model().objects.create_superuser("root")
    users = get_user_model().objects
    with tenant_context(acme):
        found = set(users.with_perm(CHANGE))
        assert found == {member, root}
        assert set(users.with_perm(CHANGE, include_superusers=False)) == {member}
        permission = Permission.objects.get(codename="change_project")
        assert set(users.with_perm(permission, include_superusers=False)) == {member}
        assert gone in users.with_perm(CHANGE, is_active=None, include_superusers=False)
    assert viewer not in found
    assert outsider not in found
    assert deactivated not in found


def test_with_perm_leaves_out_superusers_a_tenant_restricts(acme):
    Tenant.objects.filter(pk=acme.pk).update(restrict_superusers=True)
    acme.refresh_from_db()
    viewer = get_user_model().objects.create_superuser("root")
    Membership.objects.create(user=viewer, tenant=acme, role=role("viewer"))
    get_user_model().objects.create_superuser("outsider")
    editor = person(acme, "member", name="m")
    with tenant_context(acme):
        assert set(get_user_model().objects.with_perm(CHANGE)) == {editor}
        assert set(get_user_model().objects.with_perm("testapp.view_project")) == {viewer, editor}


def test_with_perm_for_another_tenants_object_is_empty(acme, globex):
    person(acme, "owner")
    users = get_user_model().objects
    with tenant_context(acme):
        assert not users.with_perm(CHANGE, include_superusers=False, obj=Project(tenant=globex))


def test_with_perm_outside_a_tenant_stays_global(acme):
    editor = grant(person(name="e"), "change_project")
    person(acme, "owner", name="o")
    assert set(get_user_model().objects.with_perm(CHANGE, include_superusers=False)) == {editor}


def test_a_listed_permission_without_a_row_is_enumerated(acme):
    custom = Role.objects.create(tenant=acme, name="editor", permissions=["testapp.publish"])
    user = person()
    Membership.objects.create(user=user, tenant=acme, role=custom)
    with tenant_context(acme):
        assert "testapp.publish" in user.get_all_permissions()
        assert user.has_module_perms("testapp")


def test_user_model_mixin_covers_every_instance(acme, globex):
    Tenant.objects.filter(pk=acme.pk).update(restrict_superusers=True)
    acme.refresh_from_db()
    root = get_user_model().objects.create_superuser("root")
    Membership.objects.create(user=root, tenant=acme, role=role("viewer"))
    loaded = ShieldedUser.objects.get(pk=root.pk)
    with tenant_context(acme):
        assert not loaded.has_perm(CHANGE)
        assert loaded.has_perm("testapp.view_project")
        assert not loaded.has_perms([CHANGE])
        assert not loaded.has_module_perms("auth")
        assert not async_to_sync(loaded.ahas_perm)(CHANGE)
    with tenant_context(globex):
        assert loaded.has_perm(CHANGE)
    assert loaded.has_perm(CHANGE)
    ordinary = ShieldedUser.objects.create_user("ann")
    with tenant_context(acme):
        assert not ordinary.has_perm(CHANGE)
