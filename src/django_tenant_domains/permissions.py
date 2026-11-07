"""What a tenant role allows. Global Django permissions play no part inside a tenant."""

from fnmatch import fnmatchcase

from asgiref.sync import sync_to_async
from django.apps import apps

from .context import get_current_tenant
from .sharing import object_allows

# Patterns over "app_label.codename". The built-in roles are data, and projects can add others.
BUILT_IN_ROLE_PERMISSIONS = {
    "owner": ["*"],
    "admin": ["*"],
    "member": ["*.view_*", "*.add_*", "*.change_*"],
    "viewer": ["*.view_*"],
}


def _model_for(app_label: str, codename: str):
    _, _, model_name = codename.partition("_")
    try:
        return apps.get_model(app_label, model_name)
    except (LookupError, ValueError):
        return None


def _registered_models(role) -> dict:
    """Map each registered permission to its model, once per role object, so once per request.

    A custom permission's name says nothing about its model, even when it looks as if it does.
    """
    from django.contrib.auth.models import Permission

    if not hasattr(role, "_permission_models"):
        models = {}
        for row in Permission.objects.select_related("content_type"):
            key = f"{row.content_type.app_label}.{row.codename}"
            # One app can register the same codename on several models.
            models.setdefault(key, []).append(row.content_type.model_class())
        role._permission_models = models
    return role._permission_models


def _app_has_tenant_models(app_label: str) -> bool:
    try:
        config = apps.get_app_config(app_label)
    except LookupError:
        return False
    return any(getattr(model, "_is_tenant_scoped", False) for model in config.get_models())


def role_allows(role, perm: str) -> bool:
    """Whether this role grants "app_label.codename".

    A role only ever governs its tenant's data, so even an explicit entry cannot grant a
    Django-wide permission such as auth.change_user. Managing members takes "*" or an explicit
    entry, so that "*.add_*" does not let a member invite people or approve themselves.
    """
    app_label, dot, codename = perm.partition(".")
    if not dot:
        return False
    models = _registered_models(role).get(perm)
    if models is None:
        inferred = _model_for(app_label, codename)
        models = [inferred] if inferred is not None else []
    # Without a model, a permission named only in a role belongs to the app's tenant data.
    if not models and not _app_has_tenant_models(app_label):
        return False
    admin_only = any(getattr(model, "_tenant_admin_only", False) for model in models)
    # A global model among them means this name protects something outside any tenant.
    if not all(
        getattr(model, "_tenant_admin_only", False) or getattr(model, "_is_tenant_scoped", False)
        for model in models
    ):
        return False
    for pattern in role.permissions:
        if not isinstance(pattern, str):
            continue
        if pattern == perm or pattern == "*":
            return True
        if not admin_only and fnmatchcase(perm, pattern):
            return True
    return False


def role_covers(holder, granted) -> bool:
    """Whether someone with role `holder` may hand out role `granted`: only what they hold."""
    if "*" in holder.permissions:
        return True
    return all(entry in holder.permissions for entry in granted.permissions)


def membership_for(user, tenant):
    """Return the user's active membership in this tenant, or None, cached on the user object."""
    from .models import Membership

    if not getattr(user, "is_authenticated", False):
        return None
    # Attribute access, not __dict__, so that request.user's lazy wrapper shares the cache.
    cache = getattr(user, "_tenant_membership_cache", None)
    if cache is None:
        cache = {}
        user._tenant_membership_cache = cache
    if tenant.pk not in cache:
        cache[tenant.pk] = (
            Membership.objects.select_related("role")
            .filter(user=user, tenant=tenant, is_active=True)
            .first()
        )
    return cache[tenant.pk]


def user_has_tenant_perm(user, perm: str) -> bool:
    """user.has_perm, but honoring restrict_superusers for any user object."""
    answer = restricted_answer(user, perm)
    return user.has_perm(perm) if answer is None else answer


def restricted_answer(user, perm: str, obj=None):
    """The role's answer when user is a superuser in a restricted tenant, otherwise None."""
    tenant = get_current_tenant()
    if tenant is None or not tenant.restrict_superusers or not user.is_superuser:
        return None
    if obj is not None and not object_allows(perm, obj, tenant):
        return False
    membership = membership_for(user, tenant) if user.is_active else None
    return membership is not None and role_allows(membership.role, perm)


def restricted_module_answer(user, app_label: str):
    tenant = get_current_tenant()
    if tenant is None or not tenant.restrict_superusers or not user.is_superuser:
        return None
    from .backends import TenantBackend

    prefix = f"{app_label}."
    allowed = TenantBackend().get_all_permissions(user)
    return any(perm.startswith(prefix) for perm in allowed)


def shield_restricted_superuser(user) -> None:
    """Make plain has_perm calls on this one object honor restrict_superusers.

    Django answers yes for an active superuser before asking any backend, so these methods are
    replaced on the instance. Nothing is stored, and is_superuser itself stays true, because a
    later save() would otherwise remove it for good.
    """
    cls = user.__class__

    def has_perm(perm, obj=None):
        answer = restricted_answer(user, perm, obj)
        return cls.has_perm(user, perm, obj) if answer is None else answer

    def has_module_perms(app_label):
        answer = restricted_module_answer(user, app_label)
        return cls.has_module_perms(user, app_label) if answer is None else answer

    async def ahas_perm(perm, obj=None):
        return await sync_to_async(has_perm)(perm, obj)

    async def ahas_module_perms(app_label):
        return await sync_to_async(has_module_perms)(app_label)

    user.has_perm = has_perm
    user.has_module_perms = has_module_perms
    user.ahas_perm = ahas_perm
    user.ahas_module_perms = ahas_module_perms
