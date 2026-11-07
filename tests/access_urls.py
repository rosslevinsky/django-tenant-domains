from django.http import HttpResponse, JsonResponse
from django.urls import include, path
from django.views import View

from django_tenant_domains.decorators import tenant_permission_required, tenant_required
from django_tenant_domains.mixins import TenantRequiredMixin
from tests.testapp.models import Project


def inside(request, **kwargs):
    names = list(Project.objects.values_list("name", flat=True))
    return JsonResponse(
        {
            "tenant": request.tenant.slug if request.tenant else None,
            "membership": request.membership.pk if request.membership else None,
            "names": names if request.tenant else None,
        }
    )


def boom(request, **kwargs):
    raise RuntimeError("boom")


def login(request):
    return JsonResponse({"tenant": request.tenant.slug if request.tenant else None})


def plain(request):
    return JsonResponse({"tenant": request.tenant.slug if request.tenant else None})


@tenant_required
def needs_tenant(request):
    return HttpResponse("ok")


@tenant_permission_required("testapp.change_project")
def needs_change(request, **kwargs):
    return HttpResponse("ok")


@tenant_permission_required("testapp.change_project")
async def needs_change_async(request, **kwargs):
    return HttpResponse("ok")


class Mixed(TenantRequiredMixin, View):
    def get(self, request, **kwargs):
        return HttpResponse("ok")


class MixedPerm(TenantRequiredMixin, View):
    tenant_permission = "testapp.change_project"

    def get(self, request, **kwargs):
        return HttpResponse("ok")


class AsyncMixedPerm(TenantRequiredMixin, View):
    tenant_permission = "testapp.change_project"

    async def get(self, request, **kwargs):
        return HttpResponse("ok")


def plain_has_perm(request, **kwargs):
    from django.template import engines

    source = "{{ perms.testapp.change_project }}"
    rendered = engines["django"].from_string(source).render({}, request)
    return JsonResponse(
        {
            "change": request.user.has_perm("testapp.change_project"),
            "view": request.user.has_perm("testapp.view_project"),
            "both": request.user.has_perms(["testapp.change_project", "testapp.view_project"]),
            "module": request.user.has_module_perms("auth"),
            "template": rendered,
        }
    )


def context_page(request, **kwargs):
    from django.template import engines

    source = "{{ tenant.slug }}|{{ membership.role.name }}|{% for t in user_tenants %}{{ t.slug }},{% endfor %}"
    return HttpResponse(engines["django"].from_string(source).render({}, request))


urlpatterns = [
    path("", include("django_tenant_domains.urls")),
    path("plain/", plain),
    path("accounts/login/", login),
    path("reset/", login),
    path("t/<str:tenant_slug>/boom/", boom),
    path("needs-tenant/", needs_tenant),
    path("mixed-plain/", Mixed.as_view()),
    path("t/<str:tenant_slug>/", inside),
    path("t/<str:tenant_slug>/change/", needs_change),
    path("t/<str:tenant_slug>/change-async/", needs_change_async),
    path("t/<str:tenant_slug>/mixed/", Mixed.as_view()),
    path("t/<str:tenant_slug>/mixed-perm/", MixedPerm.as_view()),
    path("t/<str:tenant_slug>/mixed-async/", AsyncMixedPerm.as_view()),
    path("t/<str:tenant_slug>/plain-perm/", plain_has_perm),
    path("t/<str:tenant_slug>/context/", context_page),
]
