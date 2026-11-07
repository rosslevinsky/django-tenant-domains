from django.db import migrations


class Migration(migrations.Migration):
    """Creates LegacyNote's table outside the ORM, as an existing database would already have it."""

    dependencies = [("testapp", "0001_initial")]

    operations = [
        migrations.RunSQL(
            "CREATE TABLE testapp_legacynote "
            "(id integer PRIMARY KEY, text varchar(50), tenant_id bigint)",
            "DROP TABLE testapp_legacynote",
        )
    ]
