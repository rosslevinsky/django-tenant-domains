from asgiref.sync import sync_to_async

from .permissions import restricted_answer, restricted_module_answer


class TenantPermissionsMixin:
    """Add to your own user model, before PermissionsMixin, to honor restrict_superusers everywhere.

    TenantMiddleware already does this for request.user. This also covers users loaded by other
    code, such as in background tasks.
    """

    def has_perm(self, perm, obj=None):
        answer = restricted_answer(self, perm, obj)
        return super().has_perm(perm, obj) if answer is None else answer

    def has_module_perms(self, app_label):
        answer = restricted_module_answer(self, app_label)
        return super().has_module_perms(app_label) if answer is None else answer

    async def ahas_perm(self, perm, obj=None):
        return await sync_to_async(self.has_perm)(perm, obj)

    async def ahas_module_perms(self, app_label):
        return await sync_to_async(self.has_module_perms)(app_label)
