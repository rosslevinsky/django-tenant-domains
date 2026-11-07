# Row-level security

On Postgres, row-level security is on by default. It is the layer that raw SQL, third-party code
and a missed ORM filter cannot get around. SQLite gets the ORM filter only.

## Setup

You need two database logins, as two aliases in `DATABASES` for the same database:

```python
DATABASES = {
    # The application. An ordinary role: not a superuser, no BYPASSRLS.
    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "app", "USER": "app", ...},
    # Migrations and cross-tenant work. Owns the tables, and has BYPASSRLS.
    "migrator": {"ENGINE": "django.db.backends.postgresql", "NAME": "app", "USER": "owner", ...},
}
DATABASE_ROUTERS = ["django_tenant_domains.routers.TenantAdminRouter"]
```

```sql
CREATE ROLE app LOGIN PASSWORD '...';
CREATE ROLE owner LOGIN PASSWORD '...' BYPASSRLS;
```

Always migrate through the bypass login. That creates the policies and gives the application's
login access to the tables, including those later migrations add:

```bash
python manage.py migrate --database=migrator
python manage.py check --database default     # Fails if a policy is missing or out of date.
```

Other names: set `TENANT_DOMAINS_RLS_BYPASS_DATABASE` to the bypass alias. Never point it at the
alias the application uses. With no other Postgres alias left, nothing is protected, and a system
check fails (E013). `check --database default` also fails if the bypass login is bound by row-level
security after all (E015), since migrations and cross-tenant work would then see no rows.

`python manage.py tenant_rls` recreates policies, `--check` checks them and `--sql` prints them.
`--check`, and the `tenant_domains.E003` system check, report a table when:

- row-level security is not enabled and forced on it;
- one of the package's policies is missing, or one is there that the table should not have;
- a policy differs from what the code generates now, for example after a sharing setting changed
  and migrate has not run since;
- a policy was changed by hand since it was created, including its roles, its expression, or
  whether it is restrictive;
- on a table with shared records or a third-party link table, the `tenant_domains_keep_owner`
  trigger is missing, disabled, or does not run the package's current function with the right
  columns.

`tenant_rls --check` looks through the bypass login unless given `--database`. The system check
looks in the database of each application alias it runs for, in case one reaches a different
database.

Each policy's comment holds the fingerprints this compares, so a hand-written comment on a
package policy also counts as a change. After a Postgres major upgrade, Postgres may print a policy
differently, and the check then asks for one `tenant_rls` run. The same happens after restoring a
dump into another server, where the database logins get new internal ids.

### Turning it off

Set `TENANT_DOMAINS_RLS = False`. Then, on every protected table, run:

```sql
ALTER TABLE ... DISABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_domains_access ON ...;
DROP POLICY IF EXISTS tenant_domains_isolation ON ...;
DROP POLICY IF EXISTS tenant_domains_read ON ...;
DROP POLICY IF EXISTS tenant_domains_insert ON ...;
DROP POLICY IF EXISTS tenant_domains_update ON ...;
DROP POLICY IF EXISTS tenant_domains_delete ON ...;
DROP TRIGGER IF EXISTS tenant_domains_keep_owner ON ...;
```

Every protected table has `tenant_domains_access`. Most also have `tenant_domains_isolation`, but
tables involved in sharing or third-party records have the four per-statement policies instead,
which is why each drop says `IF EXISTS`. The trigger is on tables with shared records and on
third-party link tables. Left in place, it still lets only the bypass login change a shared record's
owner or primary key. Once every trigger is gone, `DROP FUNCTION IF EXISTS
tenant_domains_keep_owner()` removes its function. A table left enabled with no policy refuses the
application every row.

## What is covered

Every `TenantModel` table, its multi-table inheritance children, and automatic many-to-many tables
between tenant models. That includes `managed = False` tables, which the bypass login has to own.

`Tenant`, `Membership` and `Role` are not covered, because the application reads them before it
knows the tenant, and across tenants for a tenant switcher. `Invitation`, `JoinRequest` and
`StaffAccessLog` are not covered either, by row-level security or by the ORM filter. Raw SQL under
one tenant can read another tenant's invitations, including their tokens, so code that reads these
tables has to check access itself. Other tables of your own are not covered, though a join from one
into a covered table finds only the current tenant's rows.

## Requests

`TenantMiddleware` opens a transaction around the rest of the request and sets the tenant in it, so
`SET LOCAL` lasts until the response is ready. A view exception or a 5xx response rolls it back.
Any other response commits. That includes a 4xx response Django makes from `Http404`,
`PermissionDenied` or `BadRequest` raised in a middleware placed after `TenantMiddleware`: writes
that middleware made before raising stay. Because of this:

- Each request holds a database connection and an open transaction for its whole length.
- `transaction.atomic(durable=True)` fails inside a request, since a transaction is already open.
- Put `TenantMiddleware` after `SessionMiddleware`, and after any middleware with a
  `process_exception` hook. Django stops calling those hooks at the first that returns a response,
  so one placed later would make the request commit. A system check warns about this (W002). For a
  middleware written as a factory function, the check builds it once to look. If that fails, it
  warns that it cannot tell (W005).

## Streaming responses

A streaming body runs after the request's transaction has closed, so each chunk gets a transaction
of its own with the tenant set. A cursor cannot stay open across chunks. Cleanup code that runs when
a client disconnects has no tenant, so its queries find nothing. On an async streaming body, use
`in_tenant_transaction()` for queries.

A `FileResponse` of a regular file on disk is left as it is, so the server can still send the file
directly. Any other file-like body, such as a `BytesIO` or a reader of your own, is read chunk by
chunk as the tenant, like other streaming bodies.

## Async views

A plain async view raises `TenantAsyncViewError` when handling a tenant's request, because Django
cannot hold a transaction across async code. To opt in:

```python
from django_tenant_domains.async_views import in_tenant_transaction, tenant_async_view


@tenant_async_view
async def report(request):
    rows = await in_tenant_transaction(load_report_rows)
```

The function runs as sync code, in one transaction, with the tenant set. Two calls are two
transactions, so work that must succeed or fail together belongs in one function. A query outside
the helper has no tenant, so it returns no rows and writes are refused. Async views on URLs with no
tenant are not affected.

## On-commit callbacks

A `transaction.on_commit()` callback runs after its transaction has ended, so the tenant setting is
gone by then. The package therefore runs each callback in a new transaction, under the tenant that
was current when its `tenant_context` or request finished. Work there commits separately, and a
failing callback cannot undo the data already committed.

## Outside requests

`tenant_context(t)` opens a transaction (or a savepoint inside one) with the tenant set, for
management commands and tasks. Nested contexts restore the outer tenant on exit. Do not keep a
`tenant_context` open across a `yield` in a streaming body, since its transaction cannot suspend.

`no_tenant()` lifts only the ORM filter, so row-level security still returns no rows. Cross-tenant
work names the bypass login explicitly:

```python
with no_tenant():
    Project.objects.using("migrator").count()
```

`TenantModelAdmin` does this for staff.

## Connection pooling

`SET LOCAL` lasts for one transaction, so it is safe with PgBouncer in transaction or session mode,
and with Django's own connection pool. Use separate pool settings for the two logins. Statement
mode does not work, because it does not keep a transaction on one connection.

## Shared records

Tables with shared records get a separate restrictive policy for each kind of statement, and a
trigger that stops anyone but the bypass login changing a row's owner or primary key. See
[sharing.md](sharing.md).

## Views

The application's login gets no automatic access to views, because a view reads with its owner's
rights, and here that is the bypass login, which sees every tenant. If you create a view over a
tenant table, create it `WITH (security_invoker = true)` before granting access to it.

## Your own policies

The isolation policy is restrictive, so Postgres applies it on top of any policy of your own. Yours
cannot widen access. To narrow access further, declare yours `AS RESTRICTIVE` too: the package also
creates a permissive `USING (true)` policy, and Postgres would OR a permissive one with it.

## What it does not do

A foreign key constraint is checked without row-level security. So raw SQL under one tenant can
point a key at another tenant's row, though it cannot read that row. `save()` and the queryset
methods refuse this.

## Creating third-party records under row-level security

A third-party table has no tenant column, so its policy has to look for a row in the companion
table. Django's `INSERT ... RETURNING` must also pass the select policy, so the companion row has to
be visible before the insert finishes. These are the results on Postgres 18 with an ordinary role and
an auto-generated primary key. Reads were timed for one tenant's 2,000 rows out of 200,000.

| Option | Create | Raw SQL with no companion row | Count all rows | Lookup by key |
|---|---|---|---:|---:|
| Save hook adds the link after the insert | Fails | Saved, but no tenant can read it | Not timed | Not timed |
| `AFTER INSERT` trigger adds the link | Fails | Not applicable | Not timed | Not timed |
| `BEFORE INSERT` trigger, with a plain policy | Fails | Not applicable | Not timed | Not timed |
| Policy also allows rows from the current transaction, using `xmin` | Fails | Saved, but no tenant can read it | Not timed | Not timed |
| Hook takes the key first, and inserts without `RETURNING` | Django still sends `RETURNING`, so it fails | Saved, but no tenant can read it | Not timed | Not timed |
| Rows with no link are visible to every tenant | Works | Visible to every tenant, as are rows that existed before | 1,036 ms | 0.1 ms |
| `BEFORE INSERT` trigger, with a policy that calls a volatile function | Works | Linked to the current tenant | 1,186 ms | 0.2 ms |
| Hook takes the key and adds the link first, relying on the deferred foreign key | Works | Refused, when the insert policy also requires a link | 330 ms | 0.2 ms |
