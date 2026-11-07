from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.module_loading import import_string

from ...access import tenant_exempt
from .forms import NewTenantForm, SignupForm
from .services import SignupInvalid, activate, create_tenant, find_signup, start_signup

TENANT_BACKEND = "django_tenant_domains.backends.TenantBackend"


def _send_link(request, email, tenant_name, token) -> None:
    url = request.build_absolute_uri(reverse("tenant_signup:activate", args=[token]))
    send_mail(
        f"Activate {tenant_name}",
        f"Open this link to finish creating {tenant_name}:\n\n{url}\n\n"
        "If you did not ask for this, ignore this email.",
        None,
        [email],
    )


@tenant_exempt
def start(request):
    if request.user.is_authenticated:
        return _start_signed_in(request)
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        token = start_signup(data["email"], data["tenant_name"], data["slug"], form.password_hash())
        _send_link(request, data["email"], data["tenant_name"], token)
        # The same answer whether or not the email has an account.
        return render(request, "django_tenant_domains/signup/sent.html")
    login_url = redirect_to_login(request.get_full_path()).url
    context = {"form": form, "login_url": login_url}
    return render(request, "django_tenant_domains/signup/start.html", context)


def _start_signed_in(request):
    """The person's login already exists, so no email is needed and the tenant is made at once."""
    form = NewTenantForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            create_tenant(request.user, data["tenant_name"], data["slug"])
        except SignupInvalid as error:
            form.add_error("slug", str(error))
        else:
            return redirect("tenant_domains:switcher")
    context = {"form": form, "signed_in": True, "username": request.user.get_username()}
    return render(request, "django_tenant_domains/signup/start.html", context)


def _login_backend() -> str:
    """The session must name a backend that can load the user again on the next request."""
    if TENANT_BACKEND in settings.AUTHENTICATION_BACKENDS:
        return TENANT_BACKEND
    for path in settings.AUTHENTICATION_BACKENDS:
        if hasattr(import_string(path), "get_user"):
            return path
    raise ImproperlyConfigured("No AUTHENTICATION_BACKENDS entry can load a user (get_user).")


@tenant_exempt
def activation(request, token):
    """Opening the link changes nothing, because mail scanners open links too."""
    template = "django_tenant_domains/signup/activate.html"
    try:
        if request.method == "POST":
            user, _ = activate(token)
            login(request, user, backend=_login_backend())
            return redirect("tenant_domains:switcher")
        return render(request, template, {"signup": find_signup(token)})
    except SignupInvalid as error:
        return render(request, template, {"error": str(error)}, status=410)
