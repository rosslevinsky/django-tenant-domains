from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from django_tenant_domains.rls import bypass_alias
from django_tenant_domains.rls.policies import (
    grant_sql,
    missing_policies,
    policy_sql,
    sync_policies,
)


class Command(BaseCommand):
    help = "Create row-level security policies for every tenant-scoped table, or check them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Fail if a table's policies are missing or out of date.",
        )
        parser.add_argument("--sql", action="store_true", help="Print the SQL without running it.")
        parser.add_argument("--database", default=None, help="Defaults to the bypass login.")

    def handle(self, *args, check, sql, database, **options):
        alias = database or bypass_alias()
        if sql:
            connection = connections[alias]
            for statement in policy_sql(connection) + grant_sql(connection):
                self.stdout.write(f"{statement};")
            return
        if check:
            missing = missing_policies(alias)
            if missing:
                raise CommandError(
                    f"Tables with missing or out-of-date policies: {', '.join(missing)}"
                )
            self.stdout.write("Every tenant-scoped table's policies are up to date.")
            return
        sync_policies(alias)
        self.stdout.write("Policies are up to date.")
