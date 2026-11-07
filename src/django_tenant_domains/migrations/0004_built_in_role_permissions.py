from django.db import migrations

# A copy, not an import: a migration must keep meaning what it meant when it was written.
PERMISSIONS = {
    "owner": ["*"],
    "admin": ["*"],
    "member": ["*.view_*", "*.add_*", "*.change_*"],
    "viewer": ["*.view_*"],
}


def set_permissions(apps, schema_editor):
    roles = apps.get_model("django_tenant_domains", "Role").objects.using(
        schema_editor.connection.alias
    )
    for name, permissions in PERMISSIONS.items():
        roles.update_or_create(tenant=None, name=name, defaults={"permissions": permissions})


def clear_permissions(apps, schema_editor):
    roles = apps.get_model("django_tenant_domains", "Role").objects.using(
        schema_editor.connection.alias
    )
    roles.filter(tenant=None, name="viewer").delete()
    roles.filter(tenant=None).update(permissions=[])


class Migration(migrations.Migration):
    dependencies = [("django_tenant_domains", "0003_access_workflows")]
    operations = [migrations.RunPython(set_permissions, clear_permissions)]
