from django.contrib import admin

from django_tenant_domains.admin import TenantModelAdmin

from .models import Project, Task


@admin.register(Project)
class ProjectAdmin(TenantModelAdmin):
    list_display = ["name", "tenant"]
    search_fields = ["name"]


@admin.register(Task)
class TaskAdmin(TenantModelAdmin):
    list_display = ["title", "project"]
    autocomplete_fields = ["reviewer"]
