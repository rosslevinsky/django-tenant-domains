from django.db import models

from django_tenant_domains.models import TenantModel


class Contact(TenantModel):
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self) -> str:
        return self.name
