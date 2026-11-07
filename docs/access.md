# Access and permissions

## Setup

```python
MIDDLEWARE = [
    # ...
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Keep this. The package's forms rely on it to stop forged requests.
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_tenant_domains.middleware.TenantMiddleware",
]
# In place of ModelBackend, and first. It still handles passwords and global permissions.
AUTHENTICATION_BACKENDS = ["django_tenant_domains.backends.TenantBackend"]
TEMPLATES[0]["OPTIONS"]["context_processors"].append(
    "django_tenant_domains.context_processors.tenant"
)
```

```python
urlpatterns = [path("", include("django_tenant_domains.urls")), ...]
```

## Who gets in

`TenantMiddleware` checks every request, in this order:

| Situation | Result |
|---|---|
| No such tenant | 404 |
| Not signed in | Redirect to `LOGIN_URL` |
| Tenant inactive | "Inactive" page, 403. This includes superusers, who can reactivate it in the admin |
| Disabled login (`user.is_active` false) | Treated as any other non-member, below, whatever memberships it has |
| Active member | Allowed. Sets `request.membership` |
| Superuser who is not a member, unless `restrict_superusers` is on | Allowed, with a `StaffAccessLog` row for every request. `request.membership` is `None` |
| Deactivated member | Notice page, 403 |
| Non-member, and the tenant allows join requests | Join page, 403 |
| Any other non-member | 404 |

A tenant remembered in the session goes through the same checks. If they fail, the session forgets
it and the request carries on with no tenant. The admin always runs with no tenant.

Some pages must work with no tenant, even when a tenant comes from the hostname. `LOGIN_URL`, the
admin, the switcher and invitation links are exempt. For other pages, such as password reset, add
URL prefixes to `TENANT_DOMAINS_EXEMPT_PATHS`, or mark a view with `@tenant_exempt`.

Tenant addresses (slugs) are not secret. A signed-out visitor is redirected for a real tenant and gets
a 404 for an unknown one, so anyone can find out whether a slug exists. Do not rely on an address
being hard to guess.

Token authentication that happens in the view, as in Django REST Framework, is not visible to the
middleware, which sees a signed-out visitor. Authenticate in a middleware placed before
`TenantMiddleware` so that `request.user` is set. That middleware must reject disabled users
(`is_active` false), as Django's `ModelBackend` does.

A superuser in the staff admin sees every tenant, because the admin runs with no tenant. Those admin
views write no `StaffAccessLog` row. This is by design: the log records entry into a tenant's own
pages.

## Who can do what

Inside a tenant only the person's role at that tenant counts. Outside a tenant, such as in the staff
admin, only Django's global permissions and groups count. A global permission adds nothing inside a
tenant, so staff who need to work in a customer's workspace need a membership there.

| Role | View | Add | Change | Delete | Invite and approve people |
|---|---|---|---|---|---|
| owner | Yes | Yes | Yes | Yes | Yes, including new owners |
| admin | Yes | Yes | Yes | Yes | Yes, except new owners |
| member | Yes | Yes | Yes | No | No |
| viewer | Yes | No | No | No | No |

These cover `TenantModel` data and custom permissions in apps that have a `TenantModel`. A role can
never grant a Django-wide permission such as `auth.change_user`, even by listing it.

A custom role is a `Role` with a `tenant` and a list of `permissions`. Each entry is an exact name
such as `invoices.change_invoice`, or a pattern such as `*.view_*`. `*` means everything in the table
above. Patterns other than `*` never match inviting or approving people.

```python
@tenant_required
def dashboard(request, tenant_slug): ...


@tenant_permission_required("invoices.change_invoice")
def edit(request, tenant_slug): ...


class Report(TenantRequiredMixin, View):
    tenant_permission = "invoices.view_invoice"  # Optional.
```

**Other authentication backends.** `has_perm()` stays correct with any backend listed after
`TenantBackend`. `get_all_permissions()` is not: Django combines every backend's list, so another
backend that subclasses `ModelBackend` adds global permissions back into it. Check with `has_perm()`.

**Superusers at a restricted tenant.** With `restrict_superusers` on, a superuser who is a member has
only their role. `TenantMiddleware` makes this true for `request.user`, in views and templates alike.
`User.objects.with_perm()` inside such a tenant lists only members whose role grants the permission.

Django answers `has_perm()` for an active superuser before it asks any backend, so these cases still
get a yes:

- A user loaded separately, such as `User.objects.get(...)` or one in a background task. Use
  `user_has_tenant_perm(user, perm)`. With your own user model, add `TenantPermissionsMixin` from
  `django_tenant_domains.users` before `PermissionsMixin`, which covers every instance.
- Code that reads `user.is_superuser` directly, which stays true so that saving the user cannot
  remove it.

## Joining

Nobody can give another person more than they hold. An inviter can offer a role only if their own role
has `*` or lists every entry the other role has, and only an owner can create owners. Approving a
join request grants member, so it needs the same cover of the member role.

`services.py` has `invite`, `accept_invitation`, `request_to_join`, `approve_join_request` and
`decline_join_request`, and the package's URLs provide pages for them.

- The package does not send email. The invite page shows the link, and your own code can call
  `invite()` and send it. The token in the link is as sensitive as a password until it is used.
- An invitation only works for an account whose email matches, so a forwarded link is useless. This
  relies on account emails being verified, which Django does not do by itself.
- Invitations expire after `TENANT_DOMAINS_INVITATION_DAYS`, 14 by default.
- Inviting an address that already has a live invitation changes that invitation's role and keeps its
  link. The person changing it must be able to grant the role it already offers, and becomes its
  `invited_by`. On the invite page this is checked for you; your own code passes `can_grant` to
  `invite()`.
- Someone who is already an active member cannot accept an invitation to the same tenant. The
  invitation stays unused. To change a member's role, change it on their `Membership`.
- `max_users` is enforced on invite, on accept, on asking to join and on approval. A live invitation
  holds a seat, so approving a join request cannot take it. Creating a `Membership` directly, including in the admin, is not limited.

## Templates

The switcher links to `/t/<slug>/`. With tenants on subdomains or in a header, override it with links
that fit, since the hostname or header decides the tenant.

Override any of `django_tenant_domains/{base,inactive,deactivated,join_request,switcher,invite,join_requests,invitation}.html`.
