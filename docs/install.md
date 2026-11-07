# Install

The package is not on PyPI yet. Install it from GitHub:

```bash
pip install "django-tenant-domains @ git+https://github.com/rosslevinsky/django-tenant-domains" "psycopg[binary]"
```

It supports Django 5.2 on Python 3.11 to 3.14, Django 6.0 on Python 3.12 to 3.14, and Django 6.1 on
Python 3.12 to 3.15. Use Postgres 15 or newer in production. SQLite works for development, with the
ORM filter only and no row-level security.

## Settings

```python
INSTALLED_APPS = [
    # ...
    "django_tenant_domains",
]

MIDDLEWARE = [
    # ...
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_tenant_domains.middleware.TenantMiddleware",
]

AUTHENTICATION_BACKENDS = ["django_tenant_domains.backends.TenantBackend"]
DATABASE_ROUTERS = ["django_tenant_domains.routers.TenantAdminRouter"]
```

Add the context processor and URLs as in [Access and permissions](access.md#setup).

## Database logins

Row-level security is on by default on Postgres, and it needs two logins. The application's login
must be an ordinary role, and the second one owns the tables and bypasses row-level security. Follow
[Row-level security](rls.md#setup), then migrate through the second login:

```bash
python manage.py migrate --database=migrator
python manage.py check --database default
```

## Your first model

```python
# yourapp/models.py
from django.db import models
from django_tenant_domains.models import TenantModel


class Invoice(TenantModel):
    number = models.CharField(max_length=20)
```

```python
# yourapp/urls.py
path("t/<str:tenant_slug>/invoices/", views.invoices)

# yourapp/views.py
from django_tenant_domains.decorators import tenant_required


@tenant_required
def invoices(request, tenant_slug):
    return render(request, "invoices.html", {"invoices": Invoice.objects.all()})
```

`Invoice.objects` only returns the current tenant's rows, and new rows get the current tenant. See
[Filtering](filtering.md).

## Next

- [Filtering](filtering.md): tenant context, and what the filter does
- [Access and permissions](access.md): who gets in, roles, invitations and join requests
- [Row-level security](rls.md): setup, async views and connection pooling
- [Sharing](sharing.md): sharing records between tenants
- [Third-party models](third-party.md): attaching models you do not control
- [Signup](signup.md): an optional page for creating a tenant
- [Known gaps](known-gaps.md): where the filter does not reach
