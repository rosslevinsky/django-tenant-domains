# Known gaps in automatic filtering

The ORM filter is a convenience layer, and these are the places it does not reach. Row-level
security on Postgres is the backstop for most of them. `tests/test_known_gaps.py` records the
behavior of each one.

| Gap | What happens | What to do |
|---|---|---|
| Raw SQL (`.raw()`, `connection.cursor()`) | Sees every tenant | Filter by hand; row-level security covers it on Postgres |
| `.values()` from a scoped model across a relation | Stays in the tenant only along same-tenant foreign keys between scoped models | See the row on unscoped tables below |
| `.values()` from an unscoped model into a scoped one | Sees every tenant | Start the query from the scoped model |
| Aggregates from an unscoped model across a relation | Count every tenant | Start from the scoped model, or filter by tenant |
| Foreign key access (`task.project`) | Uses Django's base manager, so it is not filtered | Saves refuse keys that cross tenants |
| Admin | A plain `ModelAdmin` raises `TenantNotSet` with no tenant, and shows one tenant with one | Use `TenantModelAdmin`, which shows staff every tenant |
| `bulk_create(update_conflicts=True)` on unshared models | A conflict on a unique value can update another tenant's row. Changing `tenant` this way and any use on shared models are refused | Include `tenant` in unique constraints |
| `QuerySet.update()` under `no_tenant()`, or with an `F()` or other SQL expression | Does not check foreign keys | Use `save()`, or check the keys first |
| Deleting a record that another tenant's row points to | The cascade deletes that row too. Raw SQL, `unscoped_objects` or the cases above can create such a key | Keep foreign keys same-tenant; row-level security on Postgres refuses the delete |
| Many-to-many rows written directly on the through model | Not checked, and joins such as `values("tags__name")` then show the other tenant's record. `add()` and `set()` are checked | Use `add()` and `set()` |
| Joins through an unscoped table | A query from a scoped model can reach another tenant, as in `comments__author__comment__project__name` | Add an explicit tenant filter on the far table; row-level security covers it on Postgres |
| `refresh_from_db()` and deferred field loading | Use the base manager, so a hand-built `Project(pk=theirs)` loads their row | Do not build instances from untrusted keys; row-level security covers it on Postgres |
| Queryset built at import time | Raises `TenantNotSet` | Build querysets inside functions |
| Queryset built under one tenant, run under another | Keeps the tenant it was built under | Do not keep querysets across tenant changes |
| `ModelForm` with a foreign key to a scoped model | Raises `TenantNotSet` when the form class is defined | Declare the field with `queryset=Model.unscoped_objects.none()` and set the real queryset in `__init__` |
| `loaddata` | Writes rows without any tenant check | Treat fixtures as trusted, cross-tenant input |
| `dumpdata` | Raises `TenantNotSet` | Use `dumpdata --all` |
| `select_for_update()` by a grantee while the owner ends a share and changes the record in one transaction | The grantee can receive the new content once the lock is released. See [Ending a share before writing private content](sharing.md#ending-a-share-before-writing-private-content) | Commit `unshare()` before writing private content |
| `select_for_update()` on a shared model under row-level security | Postgres applies the table's update policy to `SELECT ... FOR UPDATE`, so records shared read-only are left out, though a plain query returns them. SQLite returns them. `update_or_create()` checks first, and refuses a read-only record as it does on SQLite | Lock only records you can change; read the rest with a plain query |
| Third-party models without row-level security | Their own managers are not filtered, and `bulk_create` leaves a row with no owner. See [Third-party models](third-party.md) | Use `external_records`, or row-level security on Postgres |
| Membership role check | `bulk_create`, `QuerySet.update()` and two simultaneous writes can skip it, because the database does not enforce it | Use `save()` for role changes |

`unscoped_objects` is an explicit escape hatch, and it reaches every tenant by design. Its
queryset `delete()` and `bulk_create()` are not checked, and its `update()` is checked only when
a tenant is current. Saving or deleting a single record it returned does still need the matching
`tenant_context`, or `no_tenant()`.
