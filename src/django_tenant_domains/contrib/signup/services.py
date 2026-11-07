"""Creates a tenant and its owner: for a new user once they prove they control the email address,
or at once for someone already signed in."""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from ...models import Membership, get_tenant_model, normalize_slug
from ...services import built_in_role
from .models import PendingSignup

DEFAULT_SIGNUP_DAYS = 3


class SignupInvalid(Exception):
    """The link is wrong or expired, or what it was for can no longer be done."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _email_taken(email: str) -> bool:
    return get_user_model().objects.filter(email__iexact=email).exists()


def start_signup(email: str, tenant_name: str, slug: str, password_hash: str) -> str:
    """Record the signup and return the token for its link, replacing any earlier one."""
    token = secrets.token_urlsafe(32)
    days = getattr(settings, "TENANT_DOMAINS_SIGNUP_DAYS", DEFAULT_SIGNUP_DAYS)
    with transaction.atomic():
        PendingSignup.objects.filter(expires_at__lte=timezone.now()).delete()
        PendingSignup.objects.update_or_create(
            email=email.casefold(),
            defaults={
                "tenant_name": tenant_name,
                "slug": normalize_slug(slug),
                "password": password_hash,
                "token_hash": _hash(token),
                "expires_at": timezone.now() + timedelta(days=days),
            },
        )
    return token


def find_signup(token: str, lock: bool = False) -> PendingSignup:
    rows = PendingSignup.objects.select_for_update() if lock else PendingSignup.objects
    found = rows.filter(token_hash=_hash(token), expires_at__gt=timezone.now()).first()
    if found is None:
        raise SignupInvalid("This link has expired or was already used.")
    return found


@transaction.atomic
def activate(token: str):
    """Create the user, the tenant and the owner's membership, and return the user and tenant."""
    # The lock and the token check are one step. A second click waits here and finds the row gone,
    # and a signup for this email made meanwhile has a new token, so its password is never used.
    signup = find_signup(token, lock=True)
    user_model = get_user_model()
    # An email on an account proves nothing about who holds it, so never attach a tenant to one.
    if _email_taken(signup.email):
        raise SignupInvalid(
            "An account with this email already exists. To add a workspace to it, sign in first "
            "and then open the signup page again."
        )
    tenant_model = get_tenant_model()
    if tenant_model.objects.filter(slug=signup.slug).exists():
        raise SignupInvalid("That workspace address was taken. Sign up again with another.")
    fields = {user_model.USERNAME_FIELD: signup.email, "email": signup.email}
    user = user_model(**fields)
    user.password = signup.password
    try:
        with transaction.atomic():
            user.save()
            tenant = tenant_model.objects.create(slug=signup.slug, name=signup.tenant_name)
    except IntegrityError as error:
        raise SignupInvalid("That address or account was taken. Sign up again.") from error
    Membership.objects.create(user=user, tenant=tenant, role=built_in_role("owner"))
    signup.delete()
    return user, tenant


@transaction.atomic
def create_tenant(user, tenant_name: str, slug: str):
    """Create a tenant owned by a user who is already signed in, and return it."""
    tenant_model = get_tenant_model()
    slug = normalize_slug(slug)
    if tenant_model.objects.filter(slug=slug).exists():
        raise SignupInvalid("That workspace address was taken. Choose another.")
    try:
        with transaction.atomic():
            tenant = tenant_model.objects.create(slug=slug, name=tenant_name)
    except IntegrityError as error:
        raise SignupInvalid("That workspace address was taken. Choose another.") from error
    Membership.objects.create(user=user, tenant=tenant, role=built_in_role("owner"))
    return tenant
