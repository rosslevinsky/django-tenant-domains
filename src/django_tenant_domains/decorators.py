from functools import wraps

from asgiref.sync import iscoroutinefunction, sync_to_async
from django.core.exceptions import PermissionDenied
from django.http import Http404

from .permissions import user_has_tenant_perm


def check_tenant(request) -> None:
    if getattr(request, "tenant", None) is None:
        raise Http404("This page needs a tenant.")


def check_tenant_permission(request, perm: str) -> None:
    check_tenant(request)
    if not user_has_tenant_perm(request.user, perm):
        raise PermissionDenied


def _guarded(view, check):
    if iscoroutinefunction(view):

        @wraps(view)
        async def wrapper(request, *args, **kwargs):
            await sync_to_async(check)(request)
            return await view(request, *args, **kwargs)

        return wrapper

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        check(request)
        return view(request, *args, **kwargs)

    return wrapper


def tenant_required(view):
    """Answer 404 unless the request has a tenant. TenantMiddleware has checked access to it."""
    return _guarded(view, check_tenant)


def tenant_permission_required(perm: str):
    """Answer 403 unless the current tenant's role grants perm, and 404 with no tenant."""

    def decorator(view):
        return _guarded(view, lambda request: check_tenant_permission(request, perm))

    return decorator
