import django
import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.forms import modelform_factory

from django_tenant_domains.models import Membership, Role, Tenant, TenantModel, get_tenant_model

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant():
    return Tenant.objects.create(slug="acme", name="Acme")


@pytest.fixture
def user():
    return get_user_model().objects.create_user("ann")


@pytest.fixture
def member_role():
    return Role.objects.get(tenant=None, name="member")


def test_default_tenant_model():
    assert get_tenant_model() is Tenant


def test_tenant_defaults(tenant):
    assert tenant.is_active is True
    assert tenant.restrict_superusers is False
    assert tenant.allow_join_requests is False
    assert tenant.max_users is None
    assert tenant.created_at is not None


def test_slug_is_normalized_on_save():
    assert Tenant.objects.create(slug=" ACME ", name="Acme").slug == "acme"


def test_database_rejects_uppercase_slug():
    with pytest.raises(IntegrityError), transaction.atomic():
        Tenant.objects.bulk_create([Tenant(slug="ACME", name="Acme")])


def test_slug_is_unique(tenant):
    with pytest.raises(IntegrityError), transaction.atomic():
        Tenant.objects.create(slug="acme", name="Other")


def test_built_in_roles_exist_after_migrate():
    names = set(Role.objects.filter(tenant=None).values_list("name", flat=True))
    assert names == {"owner", "admin", "member", "viewer"}


def test_built_in_role_names_are_unique():
    with pytest.raises(IntegrityError), transaction.atomic():
        Role.objects.create(tenant=None, name="owner")


def test_custom_role_name_is_unique_per_tenant(tenant):
    other = Tenant.objects.create(slug="other", name="Other")
    Role.objects.create(tenant=tenant, name="auditor", permissions=["invoices.view"])
    Role.objects.create(tenant=other, name="auditor")
    with pytest.raises(IntegrityError), transaction.atomic():
        Role.objects.create(tenant=tenant, name="auditor")


def test_membership_is_unique_per_user_and_tenant(tenant, user, member_role):
    Membership.objects.create(user=user, tenant=tenant, role=member_role)
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(user=user, tenant=tenant, role=member_role)


def test_user_can_belong_to_several_tenants(tenant, user, member_role):
    other = Tenant.objects.create(slug="other", name="Other")
    Membership.objects.create(user=user, tenant=tenant, role=member_role)
    Membership.objects.create(user=user, tenant=other, role=member_role)
    assert user.tenant_memberships.count() == 2


def test_membership_defaults(tenant, user, member_role):
    membership = Membership.objects.create(user=user, tenant=tenant, role=member_role)
    assert membership.is_active is True
    assert membership.last_accessed is None
    assert membership.timezone == ""


def test_membership_rejects_another_tenants_role(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    foreign_role = Role.objects.create(tenant=other, name="auditor")
    with pytest.raises(ValidationError):
        Membership.objects.create(user=user, tenant=tenant, role=foreign_role)


def test_membership_accepts_its_own_tenants_role(tenant, user):
    role = Role.objects.create(tenant=tenant, name="auditor")
    assert Membership.objects.create(user=user, tenant=tenant, role=role).role == role


def test_tenant_model_is_abstract_with_tenant_key():
    assert TenantModel._meta.abstract
    field = TenantModel._meta.get_field("tenant")
    assert isinstance(field, models.ForeignKey)
    assert field.remote_field.on_delete is models.PROTECT


def test_core_models_are_in_the_admin():
    for model in (Tenant, Membership, Role):
        assert admin.site.is_registered(model)


def test_role_cannot_move_to_another_tenant_while_in_use(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    role = Role.objects.create(tenant=tenant, name="auditor")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    role.tenant = other
    with pytest.raises(ValidationError):
        role.save()


def test_role_with_key_zero_cannot_move_while_in_use(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    role = Role.objects.create(pk=0, tenant=tenant, name="auditor")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    role.tenant = other
    with pytest.raises(ValidationError):
        role.save()


def test_unused_role_can_move_to_another_tenant(tenant):
    other = Tenant.objects.create(slug="other", name="Other")
    role = Role.objects.create(tenant=tenant, name="auditor")
    role.tenant = other
    role.save()
    assert Role.objects.get(pk=role.pk).tenant == other


def test_tenant_with_custom_role_in_use_can_be_deleted(tenant, user):
    role = Role.objects.create(tenant=tenant, name="auditor")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    tenant.delete()
    assert not Membership.objects.filter(role_id=role.pk).exists()
    assert not Role.objects.filter(pk=role.pk).exists()


def test_role_in_use_cannot_be_deleted_on_its_own(tenant, user):
    role = Role.objects.create(tenant=tenant, name="auditor")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    with pytest.raises(models.RestrictedError):
        role.delete()


def test_membership_form_reports_another_tenants_role(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    foreign_role = Role.objects.create(tenant=other, name="auditor")
    form_class = modelform_factory(Membership, fields=["user", "tenant", "role"])
    form = form_class({"user": user.pk, "tenant": tenant.pk, "role": foreign_role.pk})
    assert not form.is_valid()
    assert "role" in form.errors


def test_partial_save_checks_the_tenant_actually_stored(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    other_role = Role.objects.create(tenant=other, name="auditor")
    membership = Membership.objects.create(
        user=user, tenant=tenant, role=Role.objects.get(tenant=None, name="member")
    )
    membership.tenant = other
    membership.role = other_role
    with pytest.raises(ValidationError):
        membership.save(update_fields=["role"])


def test_partial_save_checks_the_role_actually_stored(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    role = Role.objects.create(tenant=tenant, name="auditor")
    membership = Membership.objects.create(user=user, tenant=tenant, role=role)
    membership.tenant = other
    with pytest.raises(ValidationError):
        membership.save(update_fields=["tenant"])


def test_partial_save_of_other_fields_is_not_blocked(tenant, user, member_role):
    membership = Membership.objects.create(user=user, tenant=tenant, role=member_role)
    membership.is_active = False
    membership.save(update_fields=["is_active"])
    assert Membership.objects.get(pk=membership.pk).is_active is False


def test_membership_check_ignores_a_stale_cached_role(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    role = Role.objects.create(tenant=tenant, name="auditor")
    stale = Role.objects.get(pk=role.pk)
    role.tenant = other
    role.save()
    with pytest.raises(ValidationError):
        Membership.objects.create(user=user, tenant=tenant, role=stale)


def test_role_form_reports_a_move_while_in_use(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    role = Role.objects.create(tenant=tenant, name="auditor")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    form_class = modelform_factory(Role, fields=["tenant", "name", "permissions"])
    form = form_class({"tenant": other.pk, "name": "auditor", "permissions": "[]"}, instance=role)
    assert not form.is_valid()
    assert "tenant" in form.errors


def test_partial_role_save_of_other_fields_is_not_blocked(tenant, user):
    other = Tenant.objects.create(slug="other", name="Other")
    role = Role.objects.create(tenant=tenant, name="auditor")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    role.tenant = other
    role.name = "reviewer"
    role.save(update_fields=["name"])
    stored = Role.objects.get(pk=role.pk)
    assert (stored.name, stored.tenant) == ("reviewer", tenant)


def test_save_and_delete_accept_positional_database(tenant, user, member_role):
    membership = Membership.objects.create(user=user, tenant=tenant, role=member_role)
    if django.VERSION < (6, 0):  # Django 6.0 removed positional save() arguments.
        membership.save(False, False, "default")
    membership.delete("default")
    assert not Membership.objects.filter(user=user).exists()
