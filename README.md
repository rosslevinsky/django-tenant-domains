# django-tenant-domains

Shared-schema multi-tenancy for Django. All tenants share one set of tables, queries filter by tenant
automatically, and Postgres row-level security enforces the separation underneath.

In beta. Before 1.0, a new middle version number (0.2.0) can break your code. The
[changelog](https://github.com/rosslevinsky/django-tenant-domains/blob/main/CHANGELOG.md) says what
changed in each release.

- **Automatic filtering:** `Model.objects` only sees the current tenant, and raises an error when no
  tenant is set.
- **Row-level security:** on by default on Postgres, so raw SQL and third-party code cannot read
  another tenant's rows in your tenant models either. The package's own tables for tenants,
  memberships, roles and invitations are not covered;
  [Row-level security](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/rls.md)
  lists them.
- **Tenants, roles and access:** one user can belong to many tenants, with a role in each. Includes
  invitations, join requests, a tenant switcher and logged staff access.
- **Sharing:** a tenant can grant another tenant read or read-write access to chosen records.
- **Third-party models:** attach models you do not control, with no change to their columns.
- **Optional signup:** a page for creating a tenant, with email activation for new users. Off unless
  installed.

It supports Django 5.2 on Python 3.11 to 3.14, Django 6.0 on Python 3.12 to 3.14, and Django 6.1 on
Python 3.12 to 3.15. Use Postgres 15 or newer in production. SQLite gets the filter but no row-level
security.

## Install

The package is not on PyPI yet. Install it from GitHub:

```bash
pip install "django-tenant-domains @ git+https://github.com/rosslevinsky/django-tenant-domains" "psycopg[binary]"
```

Add the app, the middleware and the backend:

```python
INSTALLED_APPS = [..., "django_tenant_domains"]
MIDDLEWARE = [..., "django_tenant_domains.middleware.TenantMiddleware"]  # after auth
AUTHENTICATION_BACKENDS = ["django_tenant_domains.backends.TenantBackend"]
DATABASE_ROUTERS = ["django_tenant_domains.routers.TenantAdminRouter"]
```

```python
# urls.py
urlpatterns = [
    path("accounts/", include("django.contrib.auth.urls")),  # tenant pages need a login page
    path("", include("django_tenant_domains.urls")),
    # ...
]
```

Django's login view also needs a `registration/login.html` template.

On Postgres, row-level security also needs a second database login that owns the tables, and
`migrate` runs through it. [Install](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/install.md) walks
through all of it.

## Use

Subclass `TenantModel`, then run `makemigrations` and `migrate`:

```python
# models.py
from django.db import models
from django_tenant_domains.models import TenantModel


class Invoice(TenantModel):
    number = models.CharField(max_length=20)
```

Every query runs inside a tenant. In `python manage.py shell`:

```python
from django.contrib.auth import get_user_model
from django_tenant_domains.context import tenant_context
from django_tenant_domains.models import Membership, Tenant
from django_tenant_domains.services import built_in_role
from yourapp.models import Invoice

acme = Tenant.objects.create(slug="acme", name="Acme")
globex = Tenant.objects.create(slug="globex", name="Globex")

with tenant_context(acme):
    Invoice.objects.create(number="1")  # saved as Acme's

with tenant_context(globex):
    Invoice.objects.count()  # 0: Acme's invoice is invisible here

# A user gets into a tenant through a membership with a role.
ann = get_user_model().objects.create_user("ann", password="choose-one")
Membership.objects.create(user=ann, tenant=acme, role=built_in_role("owner"))
```

In a request, the middleware picks the tenant from the URL and checks that the user is a member.
Anyone else gets a 404:

```python
# urls.py
path("t/<str:tenant_slug>/invoices/", views.invoices)

# views.py
from django.shortcuts import render
from django_tenant_domains.decorators import tenant_required

from .models import Invoice


@tenant_required
def invoices(request, tenant_slug):
    return render(request, "invoices.html", {"invoices": Invoice.objects.all()})
```

Signed in as `ann`, `/t/acme/invoices/` lists Acme's invoices.

## Try the example

The `example/` folder holds an address book app with two tenants and users in several roles. It runs
on SQLite:

```bash
git clone https://github.com/rosslevinsky/django-tenant-domains
cd django-tenant-domains
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cd example
python manage.py migrate && python manage.py seed_demo && python manage.py runserver
```

Sign in at http://127.0.0.1:8000/t/acme/ as `owner`, `viewer` or `outsider`, with the password
`demo-only`. [docs/ui-checklist.md](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/ui-checklist.md)
lists what each user should see.

## Documentation

- [Install](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/install.md)
- [Filtering](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/filtering.md)
- [Access and permissions](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/access.md)
- [Row-level security and connection pooling](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/rls.md)
- [Sharing](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/sharing.md)
- [Third-party models](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/third-party.md)
- [Signup](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/signup.md)
- [Known gaps](https://github.com/rosslevinsky/django-tenant-domains/blob/main/docs/known-gaps.md)

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                   # SQLite, one worker per CPU
docker compose up -d --wait
DTD_TEST_DB=postgres pytest              # Postgres on host port 55432
nox                                      # every supported Django and Python pair
```

## License

BSD 3-Clause. The full text is in the `LICENSE` file.
