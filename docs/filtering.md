# Filtering

Every model that inherits `TenantModel` belongs to one tenant, and its `objects` manager only sees
rows visible to the current tenant. On most models those are the tenant's own rows. On a
[shareable](sharing.md) model they also include rows other tenants shared with it, and
`objects.owned()` leaves those out. With no tenant set, `objects` raises `TenantNotSet`, so a
missing filter fails loudly and leaks nothing.

## The current tenant

`TenantMiddleware` sets the tenant for each request. It finds it with the resolvers in
`TENANT_DOMAINS_RESOLVERS`, which are in `django_tenant_domains.resolvers`. By default it tries the
`tenant_slug` URL argument first (`URLKwargResolver`). If the URL has none, it falls back to the
tenant remembered in the session from an earlier request (`SessionResolver`). So a page whose URL
names no tenant can still run inside one. Resolving by subdomain (`SubdomainResolver`, with
`TENANT_DOMAINS_BASE_DOMAIN`) or by header (`HeaderResolver`, with `TENANT_DOMAINS_HEADER`) is
opt-in. However the tenant is found, the user must still be allowed in, as
[Who gets in](access.md#who-gets-in) describes.

Outside a request, set it yourself:

```python
from django_tenant_domains.context import no_tenant, tenant_context

with tenant_context(acme):
    Invoice.objects.create(number="1")  # Belongs to Acme.

with no_tenant():
    Invoice.objects.all()  # Lifts the filter. Row-level security still applies.
```

The tenant follows async tasks through `contextvars`, and it is cleared when the block ends, even after
an exception. Cross-tenant work on Postgres has to connect through the bypass login, as described in
[Outside requests](rls.md#outside-requests).

## What gets checked

- A save fails with `TenantMismatch` if the record's `tenant` is not the current tenant. The
  exception is a record shared read-write with the current tenant, which it may edit but not take
  over.
- A save fails if a foreign key points at another tenant's record. The exception is a record shared
  with the current tenant from a model marked `allow_links=True`.
- `bulk_create`, `bulk_update`, `update` and `delete` stay inside the current tenant, apart from
  the same read-write sharing exception for `bulk_update` and `update`.
- Many-to-many `add()`, `remove()`, `set()` and `clear()` work only on a record the current tenant
  owns, and `add()` only links records of that same owner.

Sharing grants access, not ownership. [What each level allows](sharing.md#what-each-level-allows)
and [Links](sharing.md#links) give the rules for each operation.

`unscoped_objects` skips the filter on purpose. Django's base manager uses it, so foreign key access
such as `task.project` is not filtered. [Known gaps](known-gaps.md) lists this and the other places
the filter does not reach. Row-level security covers most of them on Postgres.

## Your own tenant model

Set `TENANT_DOMAINS_TENANT_MODEL` before your first migration to swap in a subclass of
`AbstractTenant`, as with `AUTH_USER_MODEL`.
