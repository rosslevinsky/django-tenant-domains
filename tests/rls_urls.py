from django.contrib import admin
from django.db import connection, transaction
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.template import engines
from django.template.response import TemplateResponse
from django.urls import path

from django_tenant_domains.async_views import in_tenant_transaction, tenant_async_view
from tests.testapp.models import Project, Tag


def _raw():
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM testapp_project ORDER BY name")
        return [row[0] for row in cursor.fetchall()]


def read(request, **kwargs):
    orm = list(Project.objects.values_list("name", flat=True))
    return JsonResponse({"orm": orm, "raw": _raw()})


def plain_raw(request):
    return JsonResponse({"raw": _raw()})


def write(request, **kwargs):
    Tag.objects.create(name="written")
    return HttpResponse("ok")


def write_then_boom(request, **kwargs):
    Tag.objects.create(name="written")
    raise RuntimeError("boom")


def write_then_404(request, **kwargs):
    Tag.objects.create(name="written")
    raise Http404


def template(request, **kwargs):
    source = "{% for p in projects %}{{ p.name }}{% endfor %}"
    template = engines["django"].from_string(source)
    return TemplateResponse(request, template, {"projects": Project.objects.all()})


def stream(request, **kwargs):
    def body():
        yield Project.objects.get().name + ","
        yield Project.objects.get().name

    return StreamingHttpResponse(body())


on_commit_results = []


def commit_hook(request, **kwargs):
    def receipt():
        on_commit_results.append((list(Project.objects.values_list("name", flat=True)), _raw()))

    transaction.on_commit(receipt)
    return HttpResponse("ok")


async def async_plain(request, **kwargs):
    return HttpResponse("should not run")


async def async_no_tenant(request):
    return HttpResponse("ok")


def _names():
    return list(Project.objects.values_list("name", flat=True))


@tenant_async_view
async def async_helper(request, **kwargs):
    return JsonResponse({"names": await in_tenant_transaction(_names)})


@tenant_async_view
async def async_no_helper(request, **kwargs):
    def count():
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM testapp_project")
            return cursor.fetchone()[0]

    from asgiref.sync import sync_to_async

    return JsonResponse({"count": await sync_to_async(count)()})


@tenant_async_view
async def async_write(request, **kwargs):
    await in_tenant_transaction(lambda: Tag.objects.create(name="async-kept"))
    return HttpResponse("ok")


@tenant_async_view
async def async_write_boom(request, **kwargs):
    def work():
        Tag.objects.create(name="async-gone")
        raise RuntimeError("boom")

    await in_tenant_transaction(work)
    return HttpResponse("ok")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("t/<str:tenant_slug>/read/", read),
    path("plain-raw/", plain_raw),
    path("t/<str:tenant_slug>/commit-hook/", commit_hook),
    path("t/<str:tenant_slug>/write/", write),
    path("t/<str:tenant_slug>/write-then-boom/", write_then_boom),
    path("t/<str:tenant_slug>/write-then-404/", write_then_404),
    path("t/<str:tenant_slug>/template/", template),
    path("t/<str:tenant_slug>/stream/", stream),
    path("t/<str:tenant_slug>/async-plain/", async_plain),
    path("async-no-tenant/", async_no_tenant),
    path("t/<str:tenant_slug>/async-helper/", async_helper),
    path("t/<str:tenant_slug>/async-no-helper/", async_no_helper),
    path("t/<str:tenant_slug>/async-write/", async_write),
    path("t/<str:tenant_slug>/async-write-boom/", async_write_boom),
]
