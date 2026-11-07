from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.test import AsyncClient, Client, override_settings
from django.test.utils import override_script_prefix
from django.utils import timezone

from django_tenant_domains.context import tenant_context
from django_tenant_domains.exceptions import (
    AlreadyMember,
    InvitationInvalid,
    JoinRequestDecided,
    TenantFull,
)
from django_tenant_domains.models import (
    Invitation,
    JoinRequest,
    Membership,
    StaffAccessLog,
    Tenant,
)
from django_tenant_domains.services import (
    accept_invitation,
    approve_join_request,
    built_in_role,
    decline_join_request,
    invite,
    request_to_join,
)
from tests.support import ALL_DATABASES
from tests.testapp.models import Project

# Committed for real on Postgres, so requests run under row-level security.
pytestmark = pytest.mark.django_db(transaction=True, databases=ALL_DATABASES)


@pytest.fixture(autouse=True)
def _urls():
    with override_settings(ROOT_URLCONF="tests.access_urls"):
        yield


@pytest.fixture
def acme():
    tenant = Tenant.objects.create(slug="acme", name="Acme")
    with tenant_context(tenant):
        Project.objects.create(name="acme-project")
    return tenant


@pytest.fixture
def globex():
    tenant = Tenant.objects.create(slug="globex", name="Globex")
    with tenant_context(tenant):
        Project.objects.create(name="globex-project")
    return tenant


def make_user(name="ann", **kwargs):
    kwargs.setdefault("email", f"{name}@example.com")
    return get_user_model().objects.create_user(name, **kwargs)


def join(user, tenant, role="member", **kwargs):
    return Membership.objects.create(user=user, tenant=tenant, role=built_in_role(role), **kwargs)


def signed_in(user):
    client = Client()
    client.force_login(user)
    return client


class TestOutcomes:
    def test_unknown_tenant_is_404(self):
        assert signed_in(make_user()).get("/t/nope/").status_code == 404

    def test_anonymous_goes_to_login(self, acme):
        response = Client().get("/t/acme/?x=1")
        assert response.status_code == 302
        assert "login" in response.url
        assert "next=/t/acme/%3Fx%3D1" in response.url

    def test_member_gets_in_and_sees_only_their_tenant(self, acme, globex):
        user = make_user()
        membership = join(user, acme)
        response = signed_in(user).get("/t/acme/")
        assert response.json() == {
            "tenant": "acme",
            "membership": membership.pk,
            "names": ["acme-project"],
        }

    def test_non_member_is_404(self, acme):
        assert signed_in(make_user()).get("/t/acme/").status_code == 404

    def test_member_of_another_tenant_is_404(self, acme, globex):
        user = make_user()
        join(user, globex)
        assert signed_in(user).get("/t/acme/").status_code == 404

    @override_settings(
        AUTHENTICATION_BACKENDS=[
            "django_tenant_domains.backends.TenantBackend",
            "django.contrib.auth.backends.AllowAllUsersModelBackend",
        ]
    )
    def test_disabled_login_is_treated_as_a_non_member(self, acme):
        # A backend that keeps disabled users signed in must not let them in on their membership.
        user = make_user(is_active=False)
        join(user, acme)
        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.AllowAllUsersModelBackend")
        response = client.get("/t/acme/")
        assert response.status_code == 404
        assert b"acme-project" not in response.content

    def test_non_member_gets_the_join_page_when_allowed(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(allow_join_requests=True)
        response = signed_in(make_user()).get("/t/acme/")
        assert response.status_code == 403
        assert b"/t/acme/join/" in response.content
        assert b"acme-project" not in response.content

    def test_inactive_tenant_page_for_member(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(is_active=False)
        user = make_user()
        join(user, acme)
        response = signed_in(user).get("/t/acme/")
        assert response.status_code == 403
        assert b"inactive" in response.content

    def test_inactive_tenant_page_for_superuser_too(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(is_active=False)
        root = get_user_model().objects.create_superuser("root")
        assert signed_in(root).get("/t/acme/").status_code == 403

    def test_deactivated_member_gets_a_notice(self, acme):
        user = make_user()
        join(user, acme, is_active=False)
        response = signed_in(user).get("/t/acme/")
        assert response.status_code == 403
        assert b"removed" in response.content

    def test_deactivated_member_is_not_offered_the_join_page(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(allow_join_requests=True)
        user = make_user()
        join(user, acme, is_active=False)
        assert b"removed" in signed_in(user).get("/t/acme/").content

    def test_global_staff_get_no_special_entry(self, acme):
        user = make_user(is_staff=True)
        assert signed_in(user).get("/t/acme/").status_code == 404

    def test_non_canonical_slug_is_404_for_a_non_member(self, acme):
        assert signed_in(make_user()).get("/t/ACME/").status_code == 404

    def test_non_canonical_slug_redirects_for_a_member(self, acme):
        user = make_user()
        join(user, acme)
        assert signed_in(user).get("/t/ACME/").status_code == 301


class TestRememberedTenant:
    def test_losing_membership_forgets_it_rather_than_failing(self, acme):
        user = make_user()
        membership = join(user, acme)
        client = signed_in(user)
        assert client.get("/plain/").json() == {"tenant": None}
        client.get("/t/acme/")
        assert client.get("/plain/").json() == {"tenant": "acme"}
        Membership.objects.filter(pk=membership.pk).update(is_active=False)
        assert client.get("/plain/").json() == {"tenant": None}
        assert "tenant_domains_last_tenant" not in client.session

    def test_session_cannot_be_used_to_enter_a_tenant(self, acme):
        client = signed_in(make_user())
        session = client.session
        session["tenant_domains_last_tenant"] = "acme"
        session.save()
        assert client.get("/plain/").json() == {"tenant": None}

    def test_anonymous_session_is_no_tenant(self, acme):
        client = Client()
        session = client.session
        session["tenant_domains_last_tenant"] = "acme"
        session.save()
        assert client.get("/plain/").json() == {"tenant": None}

    def test_failed_entry_does_not_replace_the_remembered_tenant(self, acme, globex):
        user = make_user()
        join(user, acme)
        client = signed_in(user)
        client.get("/t/acme/")
        client.get("/t/globex/")
        assert client.get("/plain/").json() == {"tenant": "acme"}

    def test_admin_is_outside_any_tenant(self, acme):
        root = get_user_model().objects.create_superuser("root")
        client = signed_in(root)
        client.get("/t/acme/")
        count = StaffAccessLog.objects.count()
        with override_settings(ROOT_URLCONF=_admin_urls()):
            assert client.get("/admin/").status_code == 200
        assert StaffAccessLog.objects.count() == count


def _admin_urls():
    import types

    from django.urls import path

    module = types.ModuleType("tests._admin_urls")
    module.urlpatterns = [path("admin/", django_admin.site.urls)]
    return module


class TestSuperusers:
    def test_allowed_and_logged(self, acme):
        root = get_user_model().objects.create_superuser("root")
        response = signed_in(root).get("/t/acme/?secret=1")
        assert response.json()["names"] == ["acme-project"]
        assert response.json()["membership"] is None
        log = StaffAccessLog.objects.get()
        assert (log.user, log.tenant, log.path) == (root, acme, "/t/acme/")

    def test_each_entry_is_logged(self, acme):
        client = signed_in(get_user_model().objects.create_superuser("root"))
        client.get("/t/acme/")
        client.get("/t/acme/")
        assert StaffAccessLog.objects.count() == 2

    def test_restricted_superuser_is_a_non_member(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(restrict_superusers=True)
        root = get_user_model().objects.create_superuser("root")
        assert signed_in(root).get("/t/acme/").status_code == 404
        assert not StaffAccessLog.objects.exists()

    def test_restricted_superuser_with_membership_uses_the_role(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(restrict_superusers=True)
        root = get_user_model().objects.create_superuser("root")
        join(root, acme, "viewer")
        client = signed_in(root)
        assert client.get("/t/acme/").status_code == 200
        assert client.get("/t/acme/change/").status_code == 403

    def test_superuser_who_is_a_member_is_not_logged(self, acme):
        root = get_user_model().objects.create_superuser("root")
        join(root, acme)
        signed_in(root).get("/t/acme/")
        assert not StaffAccessLog.objects.exists()

    def test_log_survives_a_failing_request(self, acme):
        client = signed_in(get_user_model().objects.create_superuser("root"))
        client.raise_request_exception = False
        assert client.get("/t/acme/boom/").status_code == 500
        assert StaffAccessLog.objects.exists()

    def test_superuser_on_the_join_page_is_logged(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(allow_join_requests=True)
        root = get_user_model().objects.create_superuser("root")
        signed_in(root).get("/t/acme/join/")
        assert StaffAccessLog.objects.count() == 1

    def test_invitation_token_never_reaches_the_log(self, acme):
        invitation = invite(acme, "root@example.com", built_in_role("member"), None)
        root = get_user_model().objects.create_superuser("root", email="root@example.com")
        client = signed_in(root)
        client.get("/t/acme/")
        client.get(f"/invitations/{invitation.token}/")
        assert not StaffAccessLog.objects.filter(path__contains=invitation.token).exists()

    def test_unrestricted_superuser_member_can_grant_owner(self, acme):
        root = get_user_model().objects.create_superuser("root")
        join(root, acme, "viewer")
        data = {"email": "new@example.com", "role": built_in_role("owner").pk}
        signed_in(root).post("/t/acme/members/invite/", data)
        assert Invitation.objects.get().role.name == "owner"

    def test_log_cannot_be_edited_in_the_admin(self):
        log_admin = django_admin.site._registry[StaffAccessLog]
        assert not log_admin.has_change_permission(None)
        assert not log_admin.has_delete_permission(None)


class TestRestrictedSuperuserShield:
    @pytest.fixture
    def root(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(restrict_superusers=True)
        root = get_user_model().objects.create_superuser("root")
        join(root, acme, "viewer")
        return root

    def test_plain_has_perm_on_request_user_follows_the_role(self, root):
        assert signed_in(root).get("/t/acme/plain-perm/").json() == {
            "change": False,
            "view": True,
            "both": False,
            "module": False,
            "template": "False",
        }

    def test_unrestricted_tenant_keeps_superuser_powers(self, acme):
        root = get_user_model().objects.create_superuser("root")
        join(root, acme, "viewer")
        assert signed_in(root).get("/t/acme/plain-perm/").json()["change"] is True

    def test_saving_the_user_keeps_them_a_superuser(self, root, rf):
        from django_tenant_domains.permissions import shield_restricted_superuser

        user = get_user_model().objects.get(pk=root.pk)
        shield_restricted_superuser(user)
        user.first_name = "changed"
        user.save()
        stored = get_user_model().objects.get(pk=root.pk)
        assert stored.is_superuser
        assert stored.first_name == "changed"

    def test_separately_loaded_user_is_not_covered(self, root, acme):
        Tenant.objects.get(pk=acme.pk)
        with tenant_context(Tenant.objects.get(pk=acme.pk)):
            assert get_user_model().objects.get(pk=root.pk).has_perm("testapp.change_project")

    def test_shield_steps_aside_outside_the_restricted_tenant(self, root, globex):
        from django_tenant_domains.permissions import shield_restricted_superuser

        user = get_user_model().objects.get(pk=root.pk)
        shield_restricted_superuser(user)
        with tenant_context(globex):
            assert user.has_perm("testapp.change_project")
        assert user.has_perm("testapp.change_project")

    def test_async_forms_follow_the_role(self, root, acme):
        from django_tenant_domains.permissions import shield_restricted_superuser

        user = get_user_model().objects.get(pk=root.pk)
        shield_restricted_superuser(user)

        async def main():
            return await user.ahas_perm("testapp.change_project"), await user.ahas_module_perms(
                "auth"
            )

        with tenant_context(Tenant.objects.get(pk=acme.pk)):
            assert async_to_sync(main)() == (False, False)


class TestLastAccessed:
    def test_written_on_first_visit(self, acme):
        user = make_user()
        membership = join(user, acme)
        signed_in(user).get("/t/acme/")
        membership.refresh_from_db()
        assert membership.last_accessed is not None

    def test_written_at_most_once_a_day(self, acme, django_assert_num_queries):
        user = make_user()
        membership = join(user, acme)
        client = signed_in(user)
        client.get("/t/acme/")
        membership.refresh_from_db()
        first = membership.last_accessed
        client.get("/t/acme/")
        membership.refresh_from_db()
        assert membership.last_accessed == first

    def test_written_again_the_next_day(self, acme):
        user = make_user()
        membership = join(user, acme)
        old = timezone.now() - timedelta(days=2)
        Membership.objects.filter(pk=membership.pk).update(last_accessed=old)
        signed_in(user).get("/t/acme/")
        membership.refresh_from_db()
        assert membership.last_accessed > old


class TestDecorators:
    def test_tenant_required_is_404_without_one(self):
        assert signed_in(make_user()).get("/needs-tenant/").status_code == 404

    def test_permission_allowed_by_role(self, acme):
        user = make_user()
        join(user, acme, "member")
        assert signed_in(user).get("/t/acme/change/").status_code == 200

    def test_permission_refused_by_role(self, acme):
        user = make_user()
        join(user, acme, "viewer")
        assert signed_in(user).get("/t/acme/change/").status_code == 403

    def test_async_view(self, acme):
        viewer = make_user()
        join(viewer, acme, "viewer")

        async def main():
            client = AsyncClient()
            await client.aforce_login(viewer)
            return await client.get("/t/acme/change-async/")

        with override_settings(TENANT_DOMAINS_RLS=False):
            assert async_to_sync(main)().status_code == 403

    def test_async_mixin(self, acme):
        member, viewer = make_user("m"), make_user("v")
        join(member, acme, "member")
        join(viewer, acme, "viewer")

        async def get(user):
            client = AsyncClient()
            await client.aforce_login(user)
            return await client.get("/t/acme/mixed-async/")

        with override_settings(TENANT_DOMAINS_RLS=False):
            assert async_to_sync(get)(member).status_code == 200
            assert async_to_sync(get)(viewer).status_code == 403

    def test_mixin(self, acme):
        user = make_user()
        join(user, acme, "viewer")
        client = signed_in(user)
        assert client.get("/mixed-plain/").status_code == 404
        assert client.get("/t/acme/mixed/").status_code == 200
        assert client.get("/t/acme/mixed-perm/").status_code == 403


def test_context_processor(acme, globex):
    user = make_user()
    join(user, acme, "viewer")
    join(user, globex)
    response = signed_in(user).get("/t/acme/context/")
    assert response.content == b"acme|viewer|acme,globex,"


def test_context_processor_skips_inactive_tenants_and_memberships(acme, globex):
    user = make_user()
    join(user, acme)
    join(user, globex, is_active=False)
    other = Tenant.objects.create(slug="off", name="Off", is_active=False)
    join(user, other)
    assert signed_in(user).get("/t/acme/context/").content == b"acme|member|acme,"


class TestMaxUsers:
    def full(self, tenant, seats=1):
        Tenant.objects.filter(pk=tenant.pk).update(max_users=seats)
        tenant.refresh_from_db()

    def test_invite_refused_when_full(self, acme):
        join(make_user(), acme)
        self.full(acme)
        with pytest.raises(TenantFull):
            invite(acme, "b@example.com", built_in_role("member"), None)

    def test_pending_invitations_hold_seats(self, acme):
        self.full(acme)
        invite(acme, "a@example.com", built_in_role("member"), None)
        with pytest.raises(TenantFull):
            invite(acme, "b@example.com", built_in_role("member"), None)

    def test_expired_invitation_frees_its_seat(self, acme):
        self.full(acme)
        invite(acme, "a@example.com", built_in_role("member"), None)
        Invitation.objects.update(expires_at=timezone.now() - timedelta(days=1))
        invite(acme, "b@example.com", built_in_role("member"), None)

    def test_inviting_the_same_address_twice_reuses_the_invitation(self, acme):
        self.full(acme)
        first = invite(acme, "a@example.com", built_in_role("member"), None)
        second = invite(acme, "A@example.com", built_in_role("viewer"), None)
        assert first.pk == second.pk
        assert second.role.name == "viewer"

    def test_accept_refused_when_full(self, acme):
        invitation = invite(acme, "ann@example.com", built_in_role("member"), None)
        join(make_user("bob"), acme)
        self.full(acme)
        with pytest.raises(TenantFull):
            accept_invitation(invitation.token, make_user())
        assert Invitation.objects.get().status == "pending"

    def test_join_request_refused_when_full(self, acme):
        join(make_user("bob"), acme)
        self.full(acme)
        with pytest.raises(TenantFull):
            request_to_join(acme, make_user())

    def test_approval_refused_when_full(self, acme):
        request = request_to_join(acme, make_user())
        join(make_user("bob"), acme)
        self.full(acme)
        with pytest.raises(TenantFull):
            approve_join_request(request, make_user("carol"))
        assert JoinRequest.objects.get().status == "pending"

    def test_approval_cannot_take_a_seat_held_by_an_invitation(self, acme):
        request = request_to_join(acme, make_user())
        join(make_user("bob"), acme)
        invite(acme, "ann2@example.com", built_in_role("member"), None)
        self.full(acme, seats=2)
        with pytest.raises(TenantFull):
            approve_join_request(request, make_user("carol"))

    def test_invitation_uses_its_own_held_seat(self, acme):
        join(make_user("bob"), acme)
        user = make_user()
        invitation = invite(acme, user.email, built_in_role("member"), None)
        self.full(acme, seats=2)
        accept_invitation(invitation.token, user)
        assert Membership.objects.filter(user=user, is_active=True).exists()

    def test_join_request_refused_when_invitations_hold_every_seat(self, acme):
        invite(acme, "ann2@example.com", built_in_role("member"), None)
        self.full(acme)
        with pytest.raises(TenantFull):
            request_to_join(acme, make_user())

    def test_deactivated_members_do_not_count(self, acme):
        join(make_user("bob"), acme, is_active=False)
        self.full(acme)
        invite(acme, "a@example.com", built_in_role("member"), None)

    def test_reactivation_takes_a_seat(self, acme):
        user = make_user()
        join(user, acme, is_active=False)
        invitation = invite(acme, user.email, built_in_role("member"), None)
        join(make_user("bob"), acme)
        self.full(acme)
        with pytest.raises(TenantFull):
            accept_invitation(invitation.token, user)


class TestInvitations:
    def test_existing_member_cannot_accept_a_new_role(self, acme):
        user = make_user()
        join(user, acme, "member")
        invitation = invite(acme, user.email, built_in_role("admin"), None)
        with pytest.raises(AlreadyMember):
            accept_invitation(invitation.token, user)
        response = signed_in(user).post(f"/invitations/{invitation.token}/")
        assert response.status_code == 409
        assert b"already a member" in response.content
        invitation.refresh_from_db()
        assert invitation.status == "pending"
        assert Membership.objects.get(user=user, tenant=acme).role.name == "member"

    def test_reinviting_needs_the_power_to_grant_the_pending_role(self, acme):
        owner = make_user("olive")
        join(owner, acme, "owner")
        admin = make_user("adam")
        join(admin, acme, "admin")
        signed_in(owner).post(
            "/t/acme/members/invite/",
            {"email": "new@example.com", "role": built_in_role("owner").pk},
        )
        data = {"email": "new@example.com", "role": built_in_role("viewer").pk}
        response = signed_in(admin).post("/t/acme/members/invite/", data)
        assert response.status_code == 403
        assert b"/invitations/" not in response.content
        invitation = Invitation.objects.get()
        assert (invitation.role.name, invitation.invited_by) == ("owner", owner)

    def test_reinviting_records_who_changed_the_invitation(self, acme):
        admin = make_user("adam")
        join(admin, acme, "admin")
        first = invite(acme, "new@example.com", built_in_role("member"), make_user("olive"))
        data = {"email": "new@example.com", "role": built_in_role("viewer").pk}
        signed_in(admin).post("/t/acme/members/invite/", data)
        invitation = Invitation.objects.get()
        assert invitation.pk == first.pk
        assert (invitation.role.name, invitation.invited_by) == ("viewer", admin)

    def test_accept_view_creates_the_membership(self, acme):
        user = make_user()
        invitation = invite(acme, user.email.upper(), built_in_role("viewer"), None)
        client = signed_in(user)
        url = f"/invitations/{invitation.token}/"
        assert b"Acme" in client.get(url).content
        assert client.post(url).status_code == 302
        membership = Membership.objects.get(user=user, tenant=acme)
        assert membership.role.name == "viewer"
        invitation.refresh_from_db()
        assert (invitation.status, invitation.decided_by) == ("accepted", user)
        assert client.get("/t/acme/").status_code == 200

    def test_forwarded_link_does_nothing_for_another_account(self, acme):
        invitation = invite(acme, "ann@example.com", built_in_role("member"), None)
        client = signed_in(make_user("mallory"))
        url = f"/invitations/{invitation.token}/"
        response = client.get(url)
        assert response.status_code == 404
        assert b"Acme" not in response.content
        assert client.post(url).status_code == 404
        assert not Membership.objects.exists()

    def test_account_with_no_email_cannot_accept(self, acme):
        invitation = invite(acme, "ann@example.com", built_in_role("member"), None)
        with pytest.raises(InvitationInvalid):
            accept_invitation(invitation.token, make_user("blank", email=""))

    def test_expired_invitation_is_refused(self, acme):
        user = make_user()
        invitation = invite(acme, user.email, built_in_role("member"), None)
        Invitation.objects.update(expires_at=timezone.now() - timedelta(days=1))
        assert signed_in(user).post(f"/invitations/{invitation.token}/").status_code == 404
        assert not Membership.objects.exists()

    def test_invitation_cannot_be_used_twice(self, acme):
        user = make_user()
        invitation = invite(acme, user.email, built_in_role("member"), None)
        accept_invitation(invitation.token, user)
        with pytest.raises(InvitationInvalid):
            accept_invitation(invitation.token, user)

    def test_anonymous_goes_to_login(self, acme):
        invitation = invite(acme, "ann@example.com", built_in_role("member"), None)
        assert Client().get(f"/invitations/{invitation.token}/").status_code == 302

    def test_invite_view_needs_the_permission(self, acme):
        user = make_user()
        join(user, acme, "member")
        client = signed_in(user)
        assert client.get("/t/acme/members/invite/").status_code == 403

    def test_admin_can_invite_but_not_grant_owner(self, acme):
        user = make_user()
        join(user, acme, "admin")
        client = signed_in(user)
        data = {"email": "new@example.com", "role": built_in_role("owner").pk}
        response = client.post("/t/acme/members/invite/", data)
        assert response.status_code == 200
        assert not Invitation.objects.exists()
        data["role"] = built_in_role("member").pk
        response = client.post("/t/acme/members/invite/", data)
        assert b"/invitations/" in response.content
        assert Invitation.objects.get().tenant == acme

    def test_owner_can_grant_owner(self, acme):
        user = make_user()
        join(user, acme, "owner")
        data = {"email": "new@example.com", "role": built_in_role("owner").pk}
        signed_in(user).post("/t/acme/members/invite/", data)
        assert Invitation.objects.get().role.name == "owner"

    def recruiter(self, acme, extra=()):
        from django_tenant_domains.models import Role

        role = Role.objects.create(
            tenant=acme,
            name="recruiter",
            permissions=["django_tenant_domains.add_invitation", *extra],
        )
        user = make_user("rec")
        Membership.objects.create(user=user, tenant=acme, role=role)
        return user

    def test_inviter_cannot_grant_more_than_they_hold(self, acme):
        client = signed_in(self.recruiter(acme))
        for name in ("owner", "admin", "member", "viewer"):
            data = {"email": f"{name}@example.com", "role": built_in_role(name).pk}
            client.post("/t/acme/members/invite/", data)
        assert not Invitation.objects.exists()

    def test_inviter_can_grant_what_they_hold(self, acme):
        client = signed_in(self.recruiter(acme, extra=["*.view_*"]))
        data = {"email": "new@example.com", "role": built_in_role("viewer").pk}
        client.post("/t/acme/members/invite/", data)
        assert Invitation.objects.get().role.name == "viewer"

    def test_admin_can_grant_a_custom_role_they_cover(self, acme):
        from django_tenant_domains.models import Role

        custom = Role.objects.create(tenant=acme, name="top", permissions=["*"])
        user = make_user()
        join(user, acme, "admin")
        data = {"email": "new@example.com", "role": custom.pk}
        signed_in(user).post("/t/acme/members/invite/", data)
        assert Invitation.objects.get().role == custom

    def test_invite_cannot_use_another_tenants_role(self, acme, globex):
        from django_tenant_domains.models import Role

        theirs = Role.objects.create(tenant=globex, name="custom", permissions=["*"])
        user = make_user()
        join(user, acme, "owner")
        data = {"email": "new@example.com", "role": theirs.pk}
        signed_in(user).post("/t/acme/members/invite/", data)
        assert not Invitation.objects.exists()


class TestJoinRequests:
    @pytest.fixture
    def open_acme(self, acme):
        Tenant.objects.filter(pk=acme.pk).update(allow_join_requests=True)
        acme.refresh_from_db()
        return acme

    def test_request_then_approve(self, open_acme):
        user = make_user()
        client = signed_in(user)
        assert client.post("/t/acme/join/").status_code == 200
        request = JoinRequest.objects.get()
        assert (request.user, request.tenant, request.status) == (user, open_acme, "pending")

        boss = make_user("boss")
        join(boss, open_acme, "admin")
        boss_client = signed_in(boss)
        assert user.username.encode() in boss_client.get("/t/acme/members/requests/").content
        url = f"/t/acme/members/requests/{request.pk}/approve/"
        assert boss_client.post(url).status_code == 302
        request.refresh_from_db()
        assert (request.status, request.decided_by) == ("approved", boss)
        assert request.decided_at is not None
        assert Membership.objects.get(user=user).role.name == "member"
        assert client.get("/t/acme/").status_code == 200

    def test_decline(self, open_acme):
        user = make_user()
        request = request_to_join(open_acme, user)
        boss = make_user("boss")
        join(boss, open_acme, "owner")
        signed_in(boss).post(f"/t/acme/members/requests/{request.pk}/decline/")
        request.refresh_from_db()
        assert request.status == "declined"
        assert not Membership.objects.filter(user=user).exists()

    def test_asking_twice_makes_one_request(self, open_acme):
        client = signed_in(make_user())
        client.post("/t/acme/join/")
        client.post("/t/acme/join/")
        assert JoinRequest.objects.count() == 1

    def test_stale_copy_cannot_change_a_decided_request(self, open_acme):
        user = make_user()
        boss = make_user("boss")
        stale = request_to_join(open_acme, user)
        fresh = JoinRequest.objects.get(pk=stale.pk)
        decline_join_request(fresh, boss)
        with pytest.raises(JoinRequestDecided):
            approve_join_request(stale, boss)
        assert not Membership.objects.filter(user=user).exists()
        assert JoinRequest.objects.get().status == "declined"

    def test_stale_decline_cannot_overwrite_an_approval(self, open_acme):
        user = make_user()
        boss = make_user("boss")
        stale = request_to_join(open_acme, user)
        approve_join_request(JoinRequest.objects.get(pk=stale.pk), boss)
        with pytest.raises(JoinRequestDecided):
            decline_join_request(stale, boss)
        assert JoinRequest.objects.get().status == "approved"

    def test_join_is_404_when_not_allowed(self, acme):
        client = signed_in(make_user())
        assert client.post("/t/acme/join/").status_code == 404
        assert not JoinRequest.objects.exists()

    def test_join_needs_login(self, open_acme):
        assert Client().post("/t/acme/join/").status_code == 302
        assert not JoinRequest.objects.exists()

    def test_deactivated_member_cannot_ask(self, open_acme):
        user = make_user()
        join(user, open_acme, is_active=False)
        assert signed_in(user).post("/t/acme/join/").status_code == 403
        assert not JoinRequest.objects.exists()

    def test_member_cannot_approve(self, open_acme):
        request = request_to_join(open_acme, make_user())
        member = make_user("member")
        join(member, open_acme, "member")
        url = f"/t/acme/members/requests/{request.pk}/approve/"
        assert signed_in(member).post(url).status_code == 403
        assert JoinRequest.objects.get().status == "pending"

    def test_non_member_cannot_approve_themselves(self, open_acme):
        user = make_user()
        request = request_to_join(open_acme, user)
        url = f"/t/acme/members/requests/{request.pk}/approve/"
        assert signed_in(user).post(url).status_code == 403
        assert not Membership.objects.filter(user=user).exists()

    def test_admin_of_another_tenant_cannot_approve(self, open_acme, globex):
        request = request_to_join(open_acme, make_user())
        boss = make_user("boss")
        join(boss, globex, "owner")
        url = f"/t/globex/members/requests/{request.pk}/approve/"
        assert signed_in(boss).post(url).status_code == 404
        assert JoinRequest.objects.get().status == "pending"

    def test_approver_who_holds_less_than_member_cannot_approve(self, open_acme):
        from django_tenant_domains.models import Role

        role = Role.objects.create(
            tenant=open_acme,
            name="clerk",
            permissions=["django_tenant_domains.change_joinrequest"],
        )
        clerk = make_user("clerk")
        Membership.objects.create(user=clerk, tenant=open_acme, role=role)
        request = request_to_join(open_acme, make_user())
        url = f"/t/acme/members/requests/{request.pk}/approve/"
        assert signed_in(clerk).post(url).status_code == 403
        assert JoinRequest.objects.get().status == "pending"

    def test_full_tenant_error_does_not_list_requests_without_view_permission(self, open_acme):
        from django_tenant_domains.models import Role

        role = Role.objects.create(
            tenant=open_acme,
            name="clerk",
            permissions=[
                "django_tenant_domains.change_joinrequest",
                "*.view_*",
                "*.add_*",
                "*.change_*",
            ],
        )
        clerk = make_user("clerk")
        Membership.objects.create(user=clerk, tenant=open_acme, role=role)
        request = request_to_join(open_acme, make_user())
        request_to_join(open_acme, make_user("secretname"))
        Tenant.objects.filter(pk=open_acme.pk).update(max_users=1)
        response = signed_in(clerk).post(f"/t/acme/members/requests/{request.pk}/approve/")
        assert response.status_code == 409
        assert b"secretname" not in response.content

    def test_approving_needs_a_post(self, open_acme):
        request = request_to_join(open_acme, make_user())
        boss = make_user("boss")
        join(boss, open_acme, "owner")
        url = f"/t/acme/members/requests/{request.pk}/approve/"
        assert signed_in(boss).get(url).status_code == 405


class TestSubdomains:
    @pytest.fixture(autouse=True)
    def _subdomains(self):
        with override_settings(
            TENANT_DOMAINS_RESOLVERS=["django_tenant_domains.resolvers.SubdomainResolver"],
            TENANT_DOMAINS_BASE_DOMAIN="example.com",
            ALLOWED_HOSTS=["*"],
        ):
            yield

    def test_login_page_does_not_redirect_to_itself(self, acme):
        response = Client(headers={"host": "acme.example.com"}).get("/accounts/login/")
        assert response.status_code == 200
        assert response.json() == {"tenant": None}

    def test_login_page_is_exempt_under_a_script_prefix_and_query(self, acme):
        client = Client(headers={"host": "acme.example.com"})
        with override_settings(LOGIN_URL="/mount/accounts/login/?from=app"):
            response = client.get("/accounts/login/", SCRIPT_NAME="/mount")
        assert response.status_code == 200

    def test_a_longer_path_is_not_the_login_page(self, acme):
        client = Client(headers={"host": "acme.example.com"})
        assert client.get("/accounts/login/extra/").status_code in (302, 404)
        assert client.get("/plain/").status_code == 302

    def test_exempt_path_setting(self, acme):
        client = Client(headers={"host": "acme.example.com"})
        assert client.get("/reset/").status_code == 302
        with override_settings(TENANT_DOMAINS_EXEMPT_PATHS=["/reset/"]):
            assert client.get("/reset/").status_code == 200

    def test_non_member_can_accept_an_invitation_on_the_tenant_host(self, acme):
        user = make_user()
        invitation = invite(acme, user.email, built_in_role("member"), None)
        client = Client(headers={"host": "acme.example.com"})
        client.force_login(user)
        url = f"/invitations/{invitation.token}/"
        assert client.get(url).status_code == 200
        assert client.post(url).status_code == 302
        assert Membership.objects.filter(user=user, tenant=acme).exists()

    def test_other_pages_still_need_access(self, acme):
        client = Client(headers={"host": "acme.example.com"})
        client.force_login(make_user())
        assert client.get("/plain/").status_code == 404


def test_csrf_is_enforced_on_decisions_when_the_middleware_is_installed(acme):
    request = request_to_join(acme, make_user())
    boss = make_user("boss")
    join(boss, acme, "owner")
    client = Client(enforce_csrf_checks=True)
    client.force_login(boss)
    middleware = [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django_tenant_domains.middleware.TenantMiddleware",
    ]
    with override_settings(MIDDLEWARE=middleware):
        url = f"/t/acme/members/requests/{request.pk}/approve/"
        assert client.post(url).status_code == 403
    assert JoinRequest.objects.get().status == "pending"


def test_switcher_links_keep_the_script_prefix(acme):
    user = make_user()
    join(user, acme)
    with override_script_prefix("/mount/"):
        content = signed_in(user).get("/tenants/").content
    assert b'href="/mount/t/acme/"' in content


def test_invitations_cannot_be_added_in_the_admin():
    assert not django_admin.site._registry[Invitation].has_add_permission(None)


def test_switcher_lists_only_my_tenants(acme, globex):
    user = make_user()
    join(user, acme)
    content = signed_in(user).get("/tenants/").content
    assert b"Acme" in content
    assert b"Globex" not in content


class TestChecks:
    def ids(self):
        return [check.id for check in run_checks()]

    def test_warns_when_model_backend_is_still_first(self):
        with override_settings(
            AUTHENTICATION_BACKENDS=[
                "django.contrib.auth.backends.ModelBackend",
                "django_tenant_domains.backends.TenantBackend",
            ]
        ):
            assert "tenant_domains.W003" in self.ids()

    def test_quiet_when_configured(self):
        assert "tenant_domains.W003" not in self.ids()
        assert "tenant_domains.E004" not in self.ids()

    def test_error_without_authentication_middleware(self):
        with override_settings(MIDDLEWARE=["django_tenant_domains.middleware.TenantMiddleware"]):
            assert "tenant_domains.E004" in self.ids()
