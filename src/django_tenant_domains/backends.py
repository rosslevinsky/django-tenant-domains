from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .context import get_current_tenant
from .models import Membership, Role
from .permissions import membership_for, role_allows
from .sharing import object_allows


class TenantBackend(ModelBackend):
    """Use in place of ModelBackend. Inside a tenant only the role counts; outside, only global.

    Django asks backends in order until one says yes, and PermissionDenied stops the search, so
    another backend listed later cannot add a global permission back inside a tenant.
    """

    def has_perm(self, user_obj, perm, obj=None):
        tenant = get_current_tenant()
        if tenant is None:
            return super().has_perm(user_obj, perm, obj)
        if user_obj.is_active and (obj is None or object_allows(perm, obj, tenant)):
            membership = membership_for(user_obj, tenant)
            if membership is not None and role_allows(membership.role, perm):
                return True
        raise PermissionDenied

    def has_module_perms(self, user_obj, app_label):
        if get_current_tenant() is None:
            return super().has_module_perms(user_obj, app_label)
        prefix = f"{app_label}."
        if any(perm.startswith(prefix) for perm in self.get_all_permissions(user_obj)):
            return True
        raise PermissionDenied

    def get_user_permissions(self, user_obj, obj=None):
        return set() if get_current_tenant() else super().get_user_permissions(user_obj, obj)

    def get_group_permissions(self, user_obj, obj=None):
        return set() if get_current_tenant() else super().get_group_permissions(user_obj, obj)

    def get_all_permissions(self, user_obj, obj=None):
        tenant = get_current_tenant()
        if tenant is None:
            return super().get_all_permissions(user_obj, obj)
        membership = membership_for(user_obj, tenant) if user_obj.is_active else None
        if membership is None:
            return set()
        names = Permission.objects.values_list("content_type__app_label", "codename")
        perms = {f"{app_label}.{codename}" for app_label, codename in names}
        # A role can also name a permission that has no database row, such as invoices.edit.
        perms.update(entry for entry in membership.role.permissions if isinstance(entry, str))
        allowed = {perm for perm in perms if role_allows(membership.role, perm)}
        if obj is None:
            return allowed
        # Asks once per action, since a foreign record's answer costs a query.
        answers = {}
        kept = set()
        for perm in allowed:
            action = perm.rpartition(".")[2].partition("_")[0]
            if action not in answers:
                answers[action] = object_allows(perm, obj, tenant)
            if answers[action]:
                kept.add(perm)
        return kept

    def with_perm(self, perm, is_active=True, include_superusers=True, obj=None):
        tenant = get_current_tenant()
        if tenant is None:
            return super().with_perm(perm, is_active, include_superusers, obj)
        if isinstance(perm, Permission):
            perm = f"{perm.content_type.app_label}.{perm.codename}"
        users = get_user_model()._default_manager
        if obj is not None and not object_allows(perm, obj, tenant):
            return users.none()
        allowed = [
            role.pk
            for role in Role.objects.filter(Q(tenant=None) | Q(tenant=tenant))
            if role_allows(role, perm)
        ]
        members = Membership.objects.filter(tenant=tenant, is_active=True, role__in=allowed)
        query = Q(pk__in=members.values("user"))
        if include_superusers and not tenant.restrict_superusers:
            query |= Q(is_superuser=True)
        if is_active is not None:
            query &= Q(is_active=is_active)
        return users.filter(query)

    # ModelBackend's async methods do not call the sync ones above, so without these an async
    # check would answer from global permissions inside a tenant.
    async def ahas_perm(self, user_obj, perm, obj=None):
        return await sync_to_async(self.has_perm)(user_obj, perm, obj)

    async def ahas_module_perms(self, user_obj, app_label):
        return await sync_to_async(self.has_module_perms)(user_obj, app_label)

    async def aget_user_permissions(self, user_obj, obj=None):
        return await sync_to_async(self.get_user_permissions)(user_obj, obj)

    async def aget_group_permissions(self, user_obj, obj=None):
        return await sync_to_async(self.get_group_permissions)(user_obj, obj)

    async def aget_all_permissions(self, user_obj, obj=None):
        return await sync_to_async(self.get_all_permissions)(user_obj, obj)
