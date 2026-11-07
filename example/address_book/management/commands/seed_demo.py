from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from address_book.models import Contact
from django_tenant_domains.context import tenant_context
from django_tenant_domains.models import Membership, Tenant
from django_tenant_domains.services import built_in_role

PASSWORD = "demo-only"


class Command(BaseCommand):
    help = "Create two workspaces and one person for each page to show. All passwords: demo-only."

    def handle(self, *args, **options):
        acme = self.tenant("acme", "Acme", allow_join_requests=True)
        globex = self.tenant("globex", "Globex")
        self.tenant("closed", "Closed Co", is_active=False)
        people = {
            "owner": (acme, "owner", True),
            "admin": (acme, "admin", True),
            "member": (acme, "member", True),
            "viewer": (acme, "viewer", True),
            "former": (acme, "member", False),
            "outsider": (globex, "member", True),
        }
        for name, (tenant, role, active) in people.items():
            Membership.objects.update_or_create(
                user=self.user(name),
                tenant=tenant,
                defaults={"role": built_in_role(role), "is_active": active},
            )
        self.user("root", is_staff=True, is_superuser=True)
        for tenant in (acme, globex):
            with tenant_context(tenant):
                Contact.objects.get_or_create(
                    name=f"{tenant.name} Front Desk",
                    defaults={"email": f"desk@{tenant.slug}.example.com", "phone": "555-0100"},
                )
        self.stdout.write("Sign in as owner, admin, member, viewer, former, outsider or root.")

    @staticmethod
    def tenant(slug, name, **fields):
        return Tenant.objects.update_or_create(slug=slug, defaults={"name": name, **fields})[0]

    @staticmethod
    def user(name, **fields):
        user, _ = get_user_model().objects.update_or_create(
            username=name, defaults={"email": f"{name}@example.com", **fields}
        )
        user.set_password(PASSWORD)
        user.save()
        return user
