from django.apps import AppConfig


class TenantDomainsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_tenant_domains"
    verbose_name = "Tenant domains"

    def ready(self):
        from django.db.models.signals import post_migrate

        from . import checks  # noqa: F401
        from .external import connect_proxies
        from .rls.policies import sync_after_migrate

        connect_proxies()
        post_migrate.connect(sync_after_migrate, sender=self)
