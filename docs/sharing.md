# Sharing

An owning tenant can give another tenant access to chosen records. The record stays the owner's.

```python
from django_tenant_domains.sharing import READ, WRITE, share, shareable, unshare


@shareable(cascade=["tasks"], allow_links=True)
class Project(TenantModel):
    name = models.CharField(max_length=100)


class Task(TenantModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
```

Run `makemigrations`, because `@shareable` generates a `ProjectShare` table, and then migrate through
the bypass login, which installs its policies.

```python
with tenant_context(acme):
    share(project, globex, WRITE, granted_by=request.user)
    unshare(project, globex)
```

- `Project.objects` now returns the tenant's own records and those shared with it.
  `Project.objects.owned()` leaves out the shared ones.
- `granted_by` records who made the latest change to a share. Calling `share()` again without it
  clears it.
- `project.shares` lists shares. Only the owner can create, change or end one, so a grantee cannot
  upgrade itself or pass a share on.

## What each level allows

| | Owner | Read-write grantee | Read-only grantee |
|---|---|---|---|
| Read | Yes | Yes | Yes |
| Edit with `save`, `update` or `bulk_update` | Yes | Yes | No |
| Change the owner or primary key | No | No | No |
| Delete | Yes | No | No |

A queryset write that includes a record the tenant cannot change raises `TenantMismatch` for the
whole write, rather than quietly skipping that row. Use `.owned()` to write to your own records only.

Many-to-many links on a shared record can only be changed by the tenant that owns it, from either
side of the relation. `clear()` from an unscoped model, such as `user.notebooks.clear()`, is refused
inside a tenant, because it would reach other tenants' links.

## Permissions

A share and a role both have to allow an action. `user.has_perm("app.view_project", shared_record)`
needs the role to allow it and any share to exist. `change_` also needs read-write access. Every
other permission on another tenant's record is refused, whatever the role says.

## Cascade

`cascade` names the reverse relations whose rows inherit a share, at the same access level. Only rows
owned by the parent's owner inherit it, so another grantee's own record on the same parent stays
private. A child model with no declared cascade stays private.

A share reaches one level. A system check fails if a child that inherits a share also declares its own
cascade (E009), since its children would stay hidden.

## Links

With `allow_links=True`, a grantee's own records can keep a foreign key to a record shared with it,
whether read-only or read-write. The linking record belongs to the grantee, and the owner cannot see
it.

- **When a share ends,** the foreign key stays and the grantee can no longer read the record behind
  it. Following the relation raises `DoesNotExist` under row-level security. On SQLite, the ORM
  filter does not cover foreign key access, so the record can still be read.
- **While another tenant's records point at a record,** deleting it raises `SharedRecordInUse`.
  Under row-level security the owner cannot see those records, so the error names none of them. The
  grantee has to clear its links, or staff can through the bypass login.

- **`select_related` on a link whose share has ended** drops the linking row itself under row-level
  security, because the join finds nothing. Load the record without it, or use `prefetch_related`.
- **Without row-level security,** following a link or joining across one still reads the other
  tenant's record, because row-level security is what enforces it. A system check fails if a model
  allows links, `TENANT_DOMAINS_RLS` is off and any configured database is Postgres (E010), because a
  check cannot tell where a router will send a model. SQLite has no row-level security, so a project
  with no Postgres database only gets a warning (W004). Do not rely on links to hide records on SQLite in production.

Deleting a parent whose cascade reaches a linked record is also refused. Under row-level security
that surfaces as a database `IntegrityError` when the transaction commits, not `SharedRecordInUse`.

## Ending a share before writing private content

Commit `unshare()` first, and only then write anything the grantee must not see.

On Postgres at the default isolation level, `select_for_update()` waits for a row lock and then returns
the newest version of the row, while the grant check keeps using the snapshot from before the wait.
So if one transaction ends a share and also changes the record, a grantee already waiting on that row
can receive the new content. This window is only open to a grantee who still held the share when the
wait began. Closing it would mean locking the share table on every read of a shared record.

## Limits

- Multi-table inheritance is not supported on shared models, and a system check fails if it is used.
- Put `@shareable` on the concrete model, not a proxy. A system check fails on a proxy (E014). A
  proxy of a shared model shares along with it.
- System checks also fail on a cascade that loops back to its own model (E008) or whose foreign key
  does not target the primary key (E007). A link over an alternate key is refused.
- `bulk_create(update_conflicts=True)` is refused on shared models.
- Delete a user who granted shares through the bypass login. Under row-level security, the default
  login without a tenant cannot see the shares whose `granted_by` it has to clear.
- Moving a record to another owner under `no_tenant()` does not change its shares. End them first.
- Raw SQL by a read-write grantee can change a shared row's foreign keys, which row-level security
  cannot check against the other table. It can only make that row inconsistent, not reveal more.
