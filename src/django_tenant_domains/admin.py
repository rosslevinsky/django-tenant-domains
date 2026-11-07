from django.contrib import admin

from .context import no_tenant
from .models import Invitation, JoinRequest, Membership, Role, StaffAccessLog, Tenant
from .rls import admin_bypass, bypass_alias_if_active


class TenantModelAdmin(admin.ModelAdmin):
    """Admin for a TenantModel. Staff work across every tenant, whichever one they last visited.

    With row-level security on, it reads and writes through the bypass database, which needs
    TenantAdminRouter in DATABASE_ROUTERS.
    """

    def get_queryset(self, request):
        # Also reached outside the views below, by the autocomplete endpoint.
        with no_tenant():
            queryset = super().get_queryset(request)
        alias = bypass_alias_if_active()
        return queryset.using(alias) if alias else queryset

    def _across_tenants(self, view, *args, **kwargs):
        with no_tenant(), admin_bypass():
            response = view(*args, **kwargs)
            # Templates render lazily, and they read querysets too.
            if hasattr(response, "render") and not response.is_rendered:
                response.render()
            return response

    def changelist_view(self, *args, **kwargs):
        return self._across_tenants(super().changelist_view, *args, **kwargs)

    def changeform_view(self, *args, **kwargs):
        return self._across_tenants(super().changeform_view, *args, **kwargs)

    def delete_view(self, *args, **kwargs):
        return self._across_tenants(super().delete_view, *args, **kwargs)

    def history_view(self, *args, **kwargs):
        return self._across_tenants(super().history_view, *args, **kwargs)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["name", "tenant"]
    list_filter = ["tenant"]
    search_fields = ["name"]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "tenant", "role", "is_active", "last_accessed"]
    list_filter = ["is_active", "role"]
    raw_id_fields = ["user", "tenant"]
    search_fields = ["tenant__slug"]


@admin.register(JoinRequest)
class JoinRequestAdmin(admin.ModelAdmin):
    list_display = ["user", "tenant", "status", "created_at"]
    list_filter = ["status"]
    raw_id_fields = ["user", "tenant", "decided_by"]


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ["email", "tenant", "role", "status", "expires_at"]
    list_filter = ["status"]
    raw_id_fields = ["tenant", "invited_by", "decided_by"]
    exclude = ["token"]  # Anyone reading the admin could otherwise take over a pending invitation.

    def has_add_permission(self, request):
        return False  # An invitation needs a token and a seat check, so it goes through invite().


@admin.register(StaffAccessLog)
class StaffAccessLogAdmin(admin.ModelAdmin):
    list_display = ["user", "tenant", "path", "timestamp"]
    raw_id_fields = ["user", "tenant"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class TenantAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug"]


# A project that swaps the Tenant model registers its own admin for it.
if not Tenant._meta.swapped:
    admin.site.register(Tenant, TenantAdmin)
