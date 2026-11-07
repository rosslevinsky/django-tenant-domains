"""Decides whether a request may enter the tenant it asked for."""

from dataclasses import dataclass

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .models import Membership, StaffAccessLog
from .permissions import membership_for


@dataclass
class Outcome:
    allowed: bool = True
    membership: object = None
    response: HttpResponse | None = None  # None with allowed False means a 404.


def allow_non_members(view):
    """Let signed-in people reach this view in a tenant they do not belong to. Used for joining."""
    view._tenant_allow_non_members = True
    return view


def tenant_exempt(view):
    """Run this view with no tenant, whatever the URL, host or session says."""
    view._tenant_exempt = True
    return view


def _page(request, tenant, name, status, **context):
    context = {"tenant": tenant, **context}
    template = f"django_tenant_domains/{name}.html"
    return Outcome(False, response=render(request, template, context, status=status))


def _touch_last_accessed(membership) -> None:
    """Write at most once a day, so an ordinary request is not a write."""
    now = timezone.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if membership.last_accessed is not None and membership.last_accessed >= day_start:
        return
    stale = Membership.objects.filter(pk=membership.pk).exclude(last_accessed__gte=day_start)
    stale.update(last_accessed=now)
    membership.last_accessed = now


def authorize(request, tenant, view) -> Outcome:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return Outcome(False, response=redirect_to_login(request.get_full_path()))
    if not tenant.is_active:
        return _page(request, tenant, "inactive", 403)

    # A disabled login counts as no one's member, even if a custom backend kept it signed in.
    membership = membership_for(user, tenant) if user.is_active else None
    if membership is not None:
        _touch_last_accessed(membership)
        return Outcome(membership=membership)
    if user.is_active and user.is_superuser and not tenant.restrict_superusers:
        # Before the view, and outside its transaction, so a failing request still leaves a record.
        StaffAccessLog.objects.create(user=user, tenant=tenant, path=request.path[:2000])
        return Outcome()
    if getattr(view, "_tenant_allow_non_members", False):
        return Outcome()
    if user.is_active and Membership.objects.filter(user=user, tenant=tenant).exists():
        return _page(request, tenant, "deactivated", 403)
    if tenant.allow_join_requests:
        try:
            join_url = reverse("tenant_domains:join", kwargs={"tenant_slug": tenant.slug})
        except NoReverseMatch:
            return Outcome(False)  # The project did not include the package's URLs.
        return _page(request, tenant, "join_request", 403, join_url=join_url)
    return Outcome(False)
