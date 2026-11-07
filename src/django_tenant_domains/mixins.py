from asgiref.sync import sync_to_async

from .decorators import check_tenant, check_tenant_permission


class TenantRequiredMixin:
    """Class-based form of @tenant_required. Set tenant_permission to also require a permission."""

    tenant_permission = None

    def _check_access(self, request) -> None:
        if self.tenant_permission is None:
            check_tenant(request)
        else:
            check_tenant_permission(request, self.tenant_permission)

    def dispatch(self, request, *args, **kwargs):
        if getattr(self, "view_is_async", False):
            return self._dispatch_async(request, *args, **kwargs)
        self._check_access(request)
        return super().dispatch(request, *args, **kwargs)

    async def _dispatch_async(self, request, *args, **kwargs):
        # Permission checks query the database, which async code cannot do directly.
        await sync_to_async(self._check_access)(request)
        return await super().dispatch(request, *args, **kwargs)
