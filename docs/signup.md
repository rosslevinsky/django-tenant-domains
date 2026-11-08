# Signup

An optional page where someone creates a tenant and becomes its owner. It stays off unless you install
it.

Before going live, put a rate limit in front of it. See [Limits](#limits).

```python
INSTALLED_APPS = [..., "django_tenant_domains.contrib.signup"]
```

```python
urlpatterns = [
    path("signup/", include("django_tenant_domains.contrib.signup.urls")),
    # ...
]
```

Run `migrate`, and set up email, since the page sends an activation link through Django's default
mailer. On Django 6.1 and later, that is the `default` entry in `MAILERS`; Django 2028 will not send
email without one. On Django 5.2 and 6.0, it is `EMAIL_BACKEND` and the other `EMAIL_` settings.
Django 6.1 refuses to start with both, so a project that supports several versions picks by version:

```python
import django

if django.VERSION >= (6, 1):
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {"host": "smtp.example.com"},
        }
    }
else:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.example.com"
```

## How it works

1. The person enters a workspace name and address, an email and a password. Only a pending row is
   created, with the password hashed.
2. They get a link by email, valid for `TENANT_DOMAINS_SIGNUP_DAYS` days (3 by default). Opening it
   changes nothing, because mail scanners open links too.
3. Pressing the button creates the user, the tenant and an owner membership, and signs them in.

Someone already signed in sees a shorter form, with only the workspace name and address. Submitting
it creates the tenant at once, with them as owner, and sends no email. This is how a person with an
account adds a second tenant. Link to the page from wherever your project lists a person's tenants.

The new user's username is their email, so a custom user model needs to work with that and with no
other required fields. The user model also needs a field named `email`, since signup looks up and
saves the address there.

Activation signs the new user in with `TenantBackend`. If that is not in `AUTHENTICATION_BACKENDS`,
it uses the first listed backend that can load users (has `get_user`).

## Limits

- An email that already has an account gets a link, but the link refuses to create anything and asks
  the person to sign in first. Django does not record whether an account's email was ever verified,
  so a tenant is never attached to an existing account through a link. If your registration pages
  do not verify email, someone can register another person's address and block their signup until
  staff remove that account.
- The form says when a workspace address is taken, so anyone can test whether one exists.
- There is no rate limit or CAPTCHA, and you need one before going live. The page sends email to any
  address it is given, so without a limit a script can use your mail server to send spam, which
  costs you money and can get your real mail marked as spam. Signed-in people can also create any
  number of tenants. Limit by IP address and by email, for example with django-ratelimit, or with
  your proxy or CDN, such as Cloudflare rate limiting or Turnstile.

Override `django_tenant_domains/signup/{start,sent,activate}.html` to change the pages.
