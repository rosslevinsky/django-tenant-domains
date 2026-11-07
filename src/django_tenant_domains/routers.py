from .rls import bypass_alias_if_active, is_admin_bypassed


class TenantAdminRouter:
    """Sends tenant models to the bypass login while TenantModelAdmin handles a request.

    Forms and validators look records up through the router. With row-level security on and no
    tenant set, the application's own login would find nothing.
    """

    def _alias(self, model):
        if is_admin_bypassed() and getattr(model, "_is_tenant_scoped", False):
            return bypass_alias_if_active()
        return None

    def db_for_read(self, model, **hints):
        return self._alias(model)

    def db_for_write(self, model, **hints):
        return self._alias(model)

    def allow_relation(self, obj1, obj2, **hints):
        return True if is_admin_bypassed() and bypass_alias_if_active() else None
