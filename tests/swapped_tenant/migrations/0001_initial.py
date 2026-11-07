from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="CustomTenant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("slug", models.SlugField(max_length=63, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                ("timezone", models.CharField(blank=True, max_length=63)),
                ("locale", models.CharField(blank=True, max_length=35)),
                ("restrict_superusers", models.BooleanField(default=False)),
                ("allow_join_requests", models.BooleanField(default=False)),
                (
                    "max_users",
                    models.PositiveIntegerField(
                        blank=True, help_text="Optional cap on active members.", null=True
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("billing_code", models.CharField(blank=True, max_length=20)),
            ],
            options={
                "abstract": False,
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("slug", models.functions.Lower("slug"))),
                        name="swapped_tenant_customtenant_slug_lowercase",
                    )
                ],
            },
        ),
    ]
