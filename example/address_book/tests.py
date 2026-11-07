from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from address_book.models import Contact
from django_tenant_domains.context import tenant_context
from django_tenant_domains.models import Membership, Role, Tenant


class TwoTenants(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", stdout=StringIO())
        cls.acme = Tenant.objects.get(slug="acme")
        cls.globex = Tenant.objects.get(slug="globex")
        with tenant_context(cls.globex):
            cls.theirs = Contact.objects.create(name="Globex Person")

    def enter(self, name):
        self.client.force_login(get_user_model().objects.get(username=name))

    def test_list_shows_only_this_tenant(self):
        self.enter("owner")
        page = self.client.get("/t/acme/").content.decode()
        self.assertIn("Acme", page)
        self.assertNotIn("Globex Person", page)

    def test_search_stays_in_this_tenant(self):
        self.enter("owner")
        page = self.client.get("/t/acme/", {"q": "Globex Person"}).content.decode()
        self.assertIn("No contacts found", page)

    def test_their_contact_is_not_found(self):
        self.enter("owner")
        for action in ("edit", "delete"):
            url = f"/t/acme/contacts/{self.theirs.pk}/{action}/"
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {"name": "x"}).status_code, 404)
        with tenant_context(self.globex):
            self.assertEqual(Contact.objects.get(pk=self.theirs.pk).name, "Globex Person")

    def test_added_contact_belongs_to_this_tenant(self):
        self.enter("owner")
        self.client.post("/t/acme/contacts/add/", {"name": "New"})
        with tenant_context(self.acme):
            self.assertTrue(Contact.objects.filter(name="New").exists())
        with tenant_context(self.globex):
            self.assertFalse(Contact.objects.filter(name="New").exists())

    def test_viewer_cannot_change_anything(self):
        self.enter("viewer")
        page = self.client.get("/t/acme/").content.decode()
        self.assertNotIn("Add a contact", page)
        self.assertEqual(self.client.post("/t/acme/contacts/add/", {"name": "x"}).status_code, 403)

    def test_links_follow_the_role(self):
        self.enter("member")
        page = self.client.get("/t/acme/").content.decode()
        self.assertIn("Edit", page)
        self.assertNotIn("Delete", page)

    def test_outsider_cannot_read(self):
        self.enter("outsider")
        self.assertNotIn("address book", self.client.get("/t/acme/").content.decode())

    def test_role_without_view_permission_cannot_list_contacts(self):
        user = get_user_model().objects.create_user(username="limited")
        role = Role.objects.create(tenant=self.acme, name="No access", permissions=[])
        Membership.objects.create(tenant=self.acme, user=user, role=role)
        with tenant_context(self.acme):
            Contact.objects.create(name="Private Contact")
        self.client.force_login(user)
        response = self.client.get("/t/acme/")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Private Contact", response.content.decode())
