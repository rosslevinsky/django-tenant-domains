"""Membership workflows, each enforcing max_users while holding a lock on the tenant."""

import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .exceptions import AlreadyMember, InvitationInvalid, JoinRequestDecided, TenantFull
from .models import Invitation, JoinRequest, Membership, Role, get_tenant_model
from .permissions import BUILT_IN_ROLE_PERMISSIONS

DEFAULT_INVITATION_DAYS = 14


def built_in_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(
        tenant=None, name=name, defaults={"permissions": BUILT_IN_ROLE_PERMISSIONS[name]}
    )
    return role


def _lock(tenant):
    # Two people joining at once would otherwise both see one seat left.
    return get_tenant_model().objects.select_for_update().get(pk=tenant.pk)


def _live_invitations(tenant):
    return Invitation.objects.filter(
        tenant=tenant, status=Invitation.Status.PENDING, expires_at__gt=timezone.now()
    )


def _check_seat(tenant, invitation=None) -> None:
    """Live invitations hold a seat. An invitation being accepted is using its own."""
    if tenant.max_users is None:
        return
    held = _live_invitations(tenant)
    if invitation is not None:
        held = held.exclude(pk=invitation.pk)
    active = Membership.objects.filter(tenant=tenant, is_active=True).count()
    if active + held.count() >= tenant.max_users:
        raise TenantFull(f"{tenant} has reached its limit of {tenant.max_users} members.")


# Every workflow locks the tenant first and the row second, so that two can never deadlock.


@transaction.atomic
def invite(tenant, email: str, role, invited_by, can_grant=None) -> Invitation:
    """Reuse a live invitation to the same address, which the caller then answers for.

    can_grant(role) says whether the caller may grant a role. Changing a live invitation needs
    it to allow the role already offered, so a lesser inviter cannot rewrite a greater one's.
    """
    locked = _lock(tenant)
    existing = _live_invitations(locked).filter(email__iexact=email).first()
    if existing is not None:
        if can_grant is not None and not can_grant(existing.role):
            raise PermissionDenied(
                "This address already has an invitation with a role you cannot grant."
            )
        existing.role = role
        existing.invited_by = invited_by
        existing.save(update_fields=["role", "invited_by"])
        return existing
    _check_seat(locked)
    days = getattr(settings, "TENANT_DOMAINS_INVITATION_DAYS", DEFAULT_INVITATION_DAYS)
    return Invitation.objects.create(
        tenant=locked,
        email=email,
        role=role,
        invited_by=invited_by,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(days=days),
    )


def _add_member(tenant, user, role, invitation=None) -> Membership:
    membership = Membership.objects.filter(tenant=tenant, user=user).first()
    if membership is not None and membership.is_active:
        return membership
    _check_seat(tenant, invitation)
    if membership is None:
        return Membership.objects.create(tenant=tenant, user=user, role=role)
    membership.role = role
    membership.is_active = True
    membership.save(update_fields=["role", "is_active"])
    return membership


@transaction.atomic
def accept_invitation(token: str, user) -> Membership:
    found = Invitation.objects.filter(token=token).select_related("tenant").first()
    if found is None:
        raise InvitationInvalid("This invitation is not valid for this account.")
    locked = _lock(found.tenant)
    invitation = _live_invitations(locked).select_for_update().filter(pk=found.pk).first()
    # The link alone is not enough, since it can be forwarded.
    if invitation is None or invitation.email.casefold() != (user.email or "").casefold():
        raise InvitationInvalid("This invitation is not valid for this account.")
    if Membership.objects.filter(tenant=locked, user=user, is_active=True).exists():
        # Accepting would leave the old role in place, so the invitation stays unused.
        raise AlreadyMember(
            f"You are already a member of {locked}. To change your role, ask someone who "
            "manages its members to change it on your membership."
        )
    membership = _add_member(locked, user, invitation.role, invitation)
    invitation.status = Invitation.Status.ACCEPTED
    invitation.decided_by = user
    invitation.save(update_fields=["status", "decided_by"])
    return membership


@transaction.atomic
def request_to_join(tenant, user) -> JoinRequest:
    locked = _lock(tenant)
    _check_seat(locked)
    join_request, _ = JoinRequest.objects.get_or_create(
        tenant=locked, user=user, status=JoinRequest.Status.PENDING
    )
    return join_request


def _pending_locked(join_request):
    """Read the request afresh, since the caller's copy may predate someone else's decision."""
    found = (
        JoinRequest.objects.select_for_update()
        .filter(pk=join_request.pk, status=JoinRequest.Status.PENDING)
        .first()
    )
    if found is None:
        raise JoinRequestDecided("This request has already been decided.")
    return found


def _decide(join_request, status, decided_by) -> None:
    join_request.status = status
    join_request.decided_by = decided_by
    join_request.decided_at = timezone.now()
    join_request.save(update_fields=["status", "decided_by", "decided_at"])


@transaction.atomic
def approve_join_request(join_request, decided_by, role=None) -> Membership:
    locked = _lock(join_request.tenant)
    current = _pending_locked(join_request)
    membership = _add_member(locked, current.user, role or built_in_role("member"))
    _decide(current, JoinRequest.Status.APPROVED, decided_by)
    return membership


@transaction.atomic
def decline_join_request(join_request, decided_by) -> None:
    _lock(join_request.tenant)
    _decide(_pending_locked(join_request), JoinRequest.Status.DECLINED, decided_by)
