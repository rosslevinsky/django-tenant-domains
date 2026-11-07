from django.utils.functional import SimpleLazyObject

from .models import Membership


def _user_tenants(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return []
    memberships = Membership.objects.filter(user=user, is_active=True, tenant__is_active=True)
    return [membership.tenant for membership in memberships.select_related("tenant")]


def tenant(request):
    """Add tenant, membership and user_tenants, which queries only if a template uses it."""
    return {
        "tenant": getattr(request, "tenant", None),
        "membership": getattr(request, "membership", None),
        "user_tenants": SimpleLazyObject(lambda: _user_tenants(request)),
    }
