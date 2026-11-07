from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import get_script_prefix, reverse
from django.views.decorators.http import require_POST

from .access import allow_non_members, tenant_exempt
from .decorators import tenant_permission_required
from .exceptions import AlreadyMember, InvitationInvalid, JoinRequestDecided, TenantFull
from .models import Invitation, JoinRequest, Membership, Role
from .permissions import role_covers, user_has_tenant_perm
from .services import (
    accept_invitation,
    approve_join_request,
    built_in_role,
    decline_join_request,
    invite,
    request_to_join,
)


@tenant_exempt
@login_required
def switcher(request):
    context = {"prefix": get_script_prefix()}
    return render(request, "django_tenant_domains/switcher.html", context)


@allow_non_members
@login_required
def join(request, tenant_slug):
    tenant = request.tenant
    if not tenant.allow_join_requests:
        raise Http404
    known = Membership.objects.filter(user=request.user, tenant=tenant).first()
    if known is not None and not known.is_active:
        return render(request, "django_tenant_domains/deactivated.html", status=403)
    context, status = {}, 200
    if known is not None:
        context["member"] = True
    elif request.method == "POST":
        try:
            request_to_join(tenant, request.user)
            context["sent"] = True
        except TenantFull as error:
            context["error"], status = str(error), 409
    elif JoinRequest.objects.filter(
        tenant=tenant, user=request.user, status=JoinRequest.Status.PENDING
    ).exists():
        context["sent"] = True
    context["join_url"] = request.path
    return render(request, "django_tenant_domains/join_request.html", context, status=status)


def _can_grant(request, role) -> bool:
    """Only what the granter holds, so nobody can invite an account of theirs into more power."""
    if request.user.is_superuser and not request.tenant.restrict_superusers:
        return True
    holder = request.membership
    if holder is None:
        return False
    if role.tenant_id is None and role.name == "owner":
        return holder.role.tenant_id is None and holder.role.name == "owner"
    return role_covers(holder.role, role)


class InviteForm(forms.Form):
    email = forms.EmailField()
    role = forms.ModelChoiceField(queryset=Role.objects.none())

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        roles = Role.objects.filter(Q(tenant=None) | Q(tenant=request.tenant))
        allowed = [role.pk for role in roles if _can_grant(request, role)]
        self.fields["role"].queryset = roles.filter(pk__in=allowed).order_by("name")


@tenant_permission_required("django_tenant_domains.add_invitation")
def invite_member(request, tenant_slug):
    form = InviteForm(request.POST or None, request=request)
    context, status = {"form": form}, 200
    if request.method == "POST" and form.is_valid():
        try:
            invitation = invite(
                request.tenant,
                form.cleaned_data["email"],
                form.cleaned_data["role"],
                request.user,
                can_grant=lambda role: _can_grant(request, role),
            )
            url = reverse("tenant_domains:invitation", kwargs={"token": invitation.token})
            context["invitation_url"] = request.build_absolute_uri(url)
        except TenantFull as error:
            context["error"], status = str(error), 409
        except PermissionDenied as error:
            context["error"], status = str(error), 403
    return render(request, "django_tenant_domains/invite.html", context, status=status)


@tenant_permission_required("django_tenant_domains.view_joinrequest")
def join_requests(request, tenant_slug):
    pending = JoinRequest.objects.filter(tenant=request.tenant, status=JoinRequest.Status.PENDING)
    context = {"join_requests": pending.select_related("user"), "can_view": True}
    return render(request, "django_tenant_domains/join_requests.html", context)


@require_POST
@tenant_permission_required("django_tenant_domains.change_joinrequest")
def decide_join_request(request, tenant_slug, pk, decision):
    # Scoped by tenant, because this table is not covered by row-level security.
    join_request = get_object_or_404(
        JoinRequest, pk=pk, tenant=request.tenant, status=JoinRequest.Status.PENDING
    )
    if decision != "decline" and not _can_grant(request, built_in_role("member")):
        raise PermissionDenied
    try:
        if decision == "decline":
            decline_join_request(join_request, request.user)
        else:
            approve_join_request(join_request, request.user)
    except JoinRequestDecided:
        pass  # Someone else got there first, and the list will show that.
    except TenantFull as error:
        context = {"error": str(error)}
        if user_has_tenant_perm(request.user, "django_tenant_domains.view_joinrequest"):
            pending = JoinRequest.objects.filter(
                tenant=request.tenant, status=JoinRequest.Status.PENDING
            )
            context.update(join_requests=pending.select_related("user"), can_view=True)
        return render(request, "django_tenant_domains/join_requests.html", context, status=409)
    return redirect("tenant_domains:join_requests", tenant_slug=request.tenant.slug)


@tenant_exempt
@login_required
def invitation(request, token):
    found = Invitation.objects.select_related("tenant").filter(token=token).first()
    usable = (
        found is not None
        and found.is_live
        and found.email.casefold() == (request.user.email or "").casefold()
    )
    if not usable:
        # The same page either way, so a wrong account learns nothing about the invitation.
        return render(
            request, "django_tenant_domains/invitation.html", {"invalid": True}, status=404
        )
    context, status = {"invitation": found}, 200
    if request.method == "POST":
        try:
            accept_invitation(token, request.user)
            return redirect("tenant_domains:switcher")
        except (TenantFull, AlreadyMember) as error:
            context["error"], status = str(error), 409
        except InvitationInvalid as error:
            raise PermissionDenied from error
    return render(request, "django_tenant_domains/invitation.html", context, status=status)
