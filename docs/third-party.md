# Third-party models

`register_external` attaches a model you do not control to tenants. A generated companion table
records which tenant owns each row and which tenants it is shared with. The third-party table gets
row-level security policies on Postgres, but no new column and no trigger.

```python
# yourapp/models.py
from blog.models import Post
from django_tenant_domains.external import register_external

PostTenantLink = register_external(Post)
```

Then run `makemigrations yourapp` and `migrate`. Registering the model after it already has rows
hides those rows from every tenant, until you give each one an owner:

```python
from django_tenant_domains.context import no_tenant
from django_tenant_domains.external import assign_owner

with no_tenant():
    assign_owner(post, acme, using="migrator")
```

When importing new rows through the bypass login, insert each row and assign its owner in one
transaction. A row left without an owner after its commit could be claimed by a tenant that had
already written a link for that key.

## Creating records

`Post.objects.create(...)` and `post.save()` work as usual inside `tenant_context`, or a request with
a tenant. A save hook reserves the primary key and writes the owner's link before the insert, which
is how the insert passes its policy. Primary keys must be auto-generated or set before saving, so
under row-level security a key from a database default (`db_default`) is refused.

Under row-level security, `bulk_create` and raw SQL inserts have no link and are refused. A library
that creates this model that way fails loudly when a tenant is active. Run that work through the
bypass login and call `assign_owner`.

## Reading records

On Postgres with row-level security, every query is bound by the database, including the library's
own views and managers. Without it, `Post.objects` is not filtered, so use `external_records`.
Its `update()` only changes rows this tenant can write:

```python
from django_tenant_domains.external import external_records

external_records(Post)  # Everything this tenant can read.
external_records(Post, write_only=True)  # Everything it can change.
```

## Sharing

```python
from django_tenant_domains.external import share, unshare
from django_tenant_domains.sharing import READ, WRITE

share(post, globex, READ)
share(post, globex, WRITE)  # Changes the access it already has.
unshare(post, globex)
```

As with `@shareable`, a read-write grantee can edit a record, but only the owner can delete it or
share it. `user.has_perm(perm, record)` follows the same links: the owner gets the role's answer, a
read-write grantee only `view_` and `change_`, a read-only grantee only `view_`, and any other tenant
nothing.

A save refuses a new or changed foreign key to a record the record's owner cannot reach: another
tenant's `TenantModel` record, under the same rules as a `TenantModel` save, or a third-party record
the owner has no link to. So a read-write grantee can't point the owner's record at the grantee's
own private records. Under row-level security a grantee can't see who owns the record, so there a
grantee can't change a foreign key at all. A key that is not changing is not checked, so a
read-write grantee can save a record whose keys point at the owner's private records.

If two third-party models with one class name register in the same app, the second raises
`ImproperlyConfigured`. Pass `register_external(Model, name="OtherTenantLink")`.

## Limits

- A many-to-many field on a registered model is protected by row-level security when Django creates
  its table. A `through` model the library declares is a third-party model of its own, so register it
  too.
- Without row-level security, two tenants saving the same explicit key at once can overwrite each
  other's row, because Django updates before it inserts. Let the database generate keys, or use
  row-level security.
- A refused `delete()` raises inside Django's own transaction block, so it marks any surrounding
  transaction for rollback. Wrap a delete you expect might be refused in `transaction.atomic()`.
- Delete a tenant through the bypass login, and delete or reassign its third-party rows first.
  Deleting the tenant removes its links, not those rows. Tenants it shared them with keep their
  access to a row with no owner, including through `assign_owner` to a new one.
- A raw SQL delete of a record fails at commit while its links exist. Use the ORM.
- Without row-level security, `bulk_create` leaves a row with no owner, and `QuerySet.update()` on the
  library's manager is not checked.
- Row-level security needs a transaction, so a create outside one raises `TenantMismatch`. Without
  row-level security, create records inside `transaction.atomic()` anyway. The record is saved before
  its owner link, so if the link fails outside a transaction, the record stays with no owner and no
  tenant can see it.
- Multi-table inheritance is refused (E011), as is registering a model that already has a tenant
  column or share table (E012).
- A tenant can tell whether a key exists by trying to claim it. It learns nothing about the record.
