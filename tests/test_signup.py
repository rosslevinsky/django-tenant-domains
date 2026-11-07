import re
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.db import connection, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from django_tenant_domains.context import tenant_context
from django_tenant_domains.contrib.signup.models import PendingSignup
from django_tenant_domains.contrib.signup.services import (
    SignupInvalid,
    _hash,
    activate,
    create_tenant,
    start_signup,
)
from django_tenant_domains.models import Membership, Tenant
from tests.support import POSTGRES, leak_db
from tests.testapp.models import Project

pytestmark = leak_db

VALID = {
    "tenant_name": "Acme",
    "slug": "Acme",
    "email": "Ann@Example.com",
    "password": "long-enough-passphrase",
}


def _token():
    return re.search(r"/activate/([^/]+)/", mail.outbox[-1].body).group(1)


def _start(client, **changes):
    return client.post(reverse("tenant_signup:start"), {**VALID, **changes})


@pytest.mark.django_db(databases=[])
def test_off_unless_installed():
    code = """
import django
from django.conf import settings
settings.configure(INSTALLED_APPS=[
    "django.contrib.contenttypes", "django.contrib.auth", "django_tenant_domains",
], ROOT_URLCONF="django_tenant_domains.urls", DEFAULT_AUTO_FIELD="django.db.models.BigAutoField")
django.setup()
from django.apps import apps
from django.urls import NoReverseMatch, reverse
assert "PendingSignup" not in {m.__name__ for m in apps.get_models()}
assert not any("signup" in c.name for c in apps.get_app_configs())
try:
    reverse("tenant_signup:start")
except NoReverseMatch:
    pass
else:
    raise AssertionError("signup has a URL")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_starting_creates_only_a_pending_row(client):
    response = _start(client)
    assert b"Check your email" in response.content
    assert not Tenant.objects.exists()
    assert not get_user_model().objects.exists()
    assert mail.outbox[-1].to == ["ann@example.com"]
    pending = PendingSignup.objects.get()
    token = _token()
    assert pending.slug == "acme"
    assert pending.token_hash != token
    assert VALID["password"] not in pending.password


def test_opening_the_link_changes_nothing(client):
    _start(client)
    response = client.get(reverse("tenant_signup:activate", args=[_token()]))
    assert b"Activate Acme" in response.content
    assert not Tenant.objects.exists()
    assert PendingSignup.objects.exists()


def test_activating_creates_the_owner_and_signs_in(client):
    _start(client)
    url = reverse("tenant_signup:activate", args=[_token()])
    response = client.post(url)
    assert response.status_code == 302
    tenant = Tenant.objects.get(slug="acme")
    user = get_user_model().objects.get(email="ann@example.com")
    assert user.check_password(VALID["password"])
    membership = Membership.objects.get(tenant=tenant, user=user)
    assert membership.role.name == "owner"
    assert not PendingSignup.objects.exists()
    assert client.get("/tenants/").status_code == 200
    assert client.get("/t/acme/stream/").status_code == 200
    with tenant_context(tenant):
        assert Project.objects.create(name="first").tenant_id == tenant.pk


class PermissionOnlyBackend:
    """Answers permissions but cannot load a user, so a session must never name it."""

    def authenticate(self, request, **credentials):
        return None

    def has_perm(self, user_obj, perm, obj=None):
        return False


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "tests.test_signup.PermissionOnlyBackend",
        "django_tenant_domains.backends.TenantBackend",
    ]
)
def test_activation_signs_in_with_a_backend_that_can_load_the_user(client):
    _start(client)
    assert client.post(reverse("tenant_signup:activate", args=[_token()])).status_code == 302
    response = client.get("/tenants/")
    assert response.status_code == 200
    assert response.wsgi_request.user.is_authenticated


def test_link_works_once(client):
    _start(client)
    url = reverse("tenant_signup:activate", args=[_token()])
    client.post(url)
    assert client.post(url).status_code == 410
    assert client.get(url).status_code == 410
    assert Tenant.objects.count() == 1


def test_wrong_and_expired_links_are_refused(client):
    assert client.post(reverse("tenant_signup:activate", args=["nope"])).status_code == 410
    _start(client)
    PendingSignup.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    assert client.post(reverse("tenant_signup:activate", args=[_token()])).status_code == 410
    assert not Tenant.objects.exists()


def test_asking_again_replaces_the_earlier_link(client):
    _start(client)
    first = _token()
    _start(client, slug="other")
    assert PendingSignup.objects.get().slug == "other"
    with pytest.raises(SignupInvalid):
        activate(first)


def test_expired_rows_are_cleared(client):
    _start(client)
    PendingSignup.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    _start(client, email="other@example.com", slug="other")
    assert [row.email for row in PendingSignup.objects.all()] == ["other@example.com"]


def test_existing_email_gets_the_same_answer_but_no_tenant(client):
    victim = get_user_model().objects.create_user("v", email="ann@example.com", password="x")
    assert b"Check your email" in _start(client).content
    response = client.post(reverse("tenant_signup:activate", args=[_token()]))
    assert response.status_code == 410
    assert b"sign in first" in response.content
    assert not Tenant.objects.exists()
    assert not Membership.objects.filter(user=victim).exists()


def test_taken_address_is_refused(client):
    Tenant.objects.create(slug="acme", name="Taken")
    assert b"That address is taken" in _start(client).content
    assert not PendingSignup.objects.exists()


def test_address_taken_while_waiting_is_refused(client):
    _start(client)
    token = _token()
    Tenant.objects.create(slug="acme", name="Taken")
    with pytest.raises(SignupInvalid, match="taken"):
        activate(token)
    assert not get_user_model().objects.exists()


@override_settings(
    AUTH_PASSWORD_VALIDATORS=[
        {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}
    ]
)
def test_password_rules_apply(client):
    assert not mail.outbox
    response = _start(client, password="short")
    assert response.status_code == 200
    assert not PendingSignup.objects.exists()


def test_password_keeps_its_spaces(client):
    _start(client, password="  long-enough-passphrase  ")
    client.post(reverse("tenant_signup:activate", args=[_token()]))
    user = get_user_model().objects.get(email="ann@example.com")
    assert user.check_password("  long-enough-passphrase  ")


@override_settings(
    AUTH_PASSWORD_VALIDATORS=[
        {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"}
    ]
)
def test_password_cannot_match_the_account(client):
    _start(client, email="longsecretpassword@example.com", password="longsecretpassword")
    assert not PendingSignup.objects.exists()


def test_name_with_a_line_break_still_sends(client):
    _start(client, tenant_name="Hello\nWorld")
    assert mail.outbox[-1].subject == "Activate Hello World"


@pytest.mark.skipif(not POSTGRES, reason="needs row locks")
def test_replacing_a_signup_during_activation_cannot_swap_the_password():
    old = start_signup("ann@example.com", "Acme", "acme", make_password("victim"))
    new = "replacement-token"
    outcome = {}

    def activate_old():
        try:
            activate(old)
            outcome["result"] = "activated"
        except SignupInvalid:
            outcome["result"] = "refused"
        finally:
            connection.close()

    # The attacker's replacement holds the row while the victim's activation waits for it.
    with transaction.atomic():
        pending = PendingSignup.objects.select_for_update().get()
        pending.password = make_password("attacker")
        pending.token_hash = _hash(new)
        pending.save()
        thread = threading.Thread(target=activate_old)
        thread.start()
        deadline = time.monotonic() + 10
        with connection.cursor() as cursor:
            waiting = False
            while not waiting and time.monotonic() < deadline:
                # Activity statistics otherwise stay frozen for the whole transaction.
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                )
                waiting = cursor.fetchone()[0] > 0
                time.sleep(0.05)
        assert waiting
    thread.join(10)
    assert outcome["result"] == "refused"
    assert not get_user_model().objects.exists()


def test_lifetime_setting_is_honored():
    with override_settings(TENANT_DOMAINS_SIGNUP_DAYS=1):
        start_signup("a@example.com", "A", "a", "hash")
    remaining = PendingSignup.objects.get().expires_at - timezone.now()
    assert remaining <= timedelta(days=1)


def _signed_in(client):
    user = get_user_model().objects.create_user("ann", email="ann@example.com", password="x")
    client.force_login(user)
    return user


def test_signed_in_form_asks_only_for_the_workspace(client):
    _signed_in(client)
    response = client.get(reverse("tenant_signup:start"))
    assert b'name="slug"' in response.content
    assert b'name="email"' not in response.content
    assert b'name="password"' not in response.content
    assert b"signed in as ann" in response.content


def test_signed_out_form_links_to_sign_in(client):
    response = client.get(reverse("tenant_signup:start"))
    assert b"next=/signup/" in response.content


def test_signed_in_user_creates_a_tenant_without_email(client):
    user = _signed_in(client)
    response = client.post(reverse("tenant_signup:start"), {"tenant_name": "Acme", "slug": "Acme"})
    assert response.status_code == 302
    tenant = Tenant.objects.get(slug="acme")
    assert Membership.objects.get(tenant=tenant, user=user).role.name == "owner"
    assert not mail.outbox
    assert not PendingSignup.objects.exists()
    assert client.get("/t/acme/stream/").status_code == 200


def test_signed_in_user_can_create_a_second_tenant(client):
    user = _signed_in(client)
    client.post(reverse("tenant_signup:start"), {"tenant_name": "Acme", "slug": "acme"})
    client.post(reverse("tenant_signup:start"), {"tenant_name": "Globex", "slug": "globex"})
    assert set(Membership.objects.filter(user=user).values_list("tenant__slug", flat=True)) == {
        "acme",
        "globex",
    }


def test_signed_in_taken_address_is_refused(client):
    _signed_in(client)
    Tenant.objects.create(slug="acme", name="Taken")
    response = client.post(reverse("tenant_signup:start"), {"tenant_name": "Acme", "slug": "acme"})
    assert b"That address is taken" in response.content
    assert Tenant.objects.count() == 1


def test_address_taken_during_creation_leaves_nothing_behind():
    user = get_user_model().objects.create_user("ann", email="ann@example.com", password="x")
    Tenant.objects.create(slug="acme", name="Taken")
    with pytest.raises(SignupInvalid, match="taken"):
        create_tenant(user, "Acme", "acme")
    assert Tenant.objects.count() == 1
    assert not Membership.objects.filter(user=user).exists()
