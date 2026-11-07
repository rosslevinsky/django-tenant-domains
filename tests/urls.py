import asyncio
import io

from django.contrib import admin
from django.http import FileResponse, JsonResponse, StreamingHttpResponse
from django.urls import include, path

from django_tenant_domains.async_views import tenant_async_view
from django_tenant_domains.context import get_current_tenant


def _slug():
    tenant = get_current_tenant()
    return tenant.slug if tenant else None


def show(request, **kwargs):
    return JsonResponse({"context": _slug(), "request": getattr(request.tenant, "slug", None)})


def boom(request, **kwargs):
    raise RuntimeError("boom")


# Tests set this so requests overlap at a known point.
barrier = None


@tenant_async_view
async def async_show(request, **kwargs):
    before = _slug()
    await barrier.wait()
    await asyncio.sleep(0)
    return JsonResponse({"before": before, "after": _slug()})


@tenant_async_view
async def async_boom(request, **kwargs):
    await barrier.wait()
    raise RuntimeError("boom")


def stream(request, **kwargs):
    def body():
        yield f"{_slug()},"
        yield f"{_slug()}"

    return StreamingHttpResponse(body())


@tenant_async_view
async def async_stream(request, **kwargs):
    async def body():
        yield f"{_slug()},"
        await asyncio.sleep(0)
        yield f"{_slug()}"

    return StreamingHttpResponse(body())


def download(request, **kwargs):
    return FileResponse(io.BytesIO(b"file-bytes"), filename="f.txt")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("signup/", include("django_tenant_domains.contrib.signup.urls")),
    path("", include("django_tenant_domains.urls")),
    path("t/<str:tenant_slug>/stream/", stream),
    path("t/<str:tenant_slug>/async-stream/", async_stream),
    path("t/<str:tenant_slug>/download/", download),
    path("t/<str:tenant_slug>/file/<str:name>/", show),
    path("x/<path:other>/t/<str:tenant_slug>/", show),
    path("org-<str:tenant_slug>/", show),
    path("t/<str:tenant_slug>/", show),
    path("t/<str:tenant_slug>/nested/page/", show),
    path("t/<str:tenant_slug>/boom/", boom),
    path("t/<str:tenant_slug>/async/", async_show),
    path("t/<str:tenant_slug>/async-boom/", async_boom),
    path("t/<str:tenant_slug>/sub/t/<str:other>/", show),
    path("plain/", show),
]
