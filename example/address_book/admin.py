from django.contrib import admin

from django_tenant_domains.admin import TenantModelAdmin

from .models import Contact

admin.site.register(Contact, TenantModelAdmin)
