from django.apps import AppConfig


class SignupConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_tenant_domains.contrib.signup"
    label = "tenant_domains_signup"
    verbose_name = "Tenant signup"
