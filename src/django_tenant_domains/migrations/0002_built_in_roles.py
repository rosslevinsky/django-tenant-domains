from django.db import migrations

BUILT_IN_ROLES = ["owner", "admin", "member"]


def create_roles(apps, schema_editor):
    role_model = apps.get_model("django_tenant_domains", "Role")
    for name in BUILT_IN_ROLES:
        role_model.objects.using(schema_editor.connection.alias).get_or_create(
            tenant=None, name=name
        )


def delete_roles(apps, schema_editor):
    role_model = apps.get_model("django_tenant_domains", "Role")
    roles = role_model.objects.using(schema_editor.connection.alias)
    roles.filter(tenant=None, name__in=BUILT_IN_ROLES).delete()


class Migration(migrations.Migration):
    dependencies = [("django_tenant_domains", "0001_initial")]
    operations = [migrations.RunPython(create_roles, delete_roles)]
