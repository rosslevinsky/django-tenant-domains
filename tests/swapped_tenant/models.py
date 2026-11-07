from django.db import models

from django_tenant_domains.models import AbstractTenant


class CustomTenant(AbstractTenant):
    billing_code = models.CharField(max_length=20, blank=True)
