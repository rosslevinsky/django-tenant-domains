import asyncio
import io

import pytest
from asgiref.sync import async_to_sync
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.test import AsyncClient, Client, RequestFactory, override_settings

from django_tenant_domains.access import Outcome
from django_tenant_domains.context import get_current_tenant, tenant_context
from django_tenant_domains.middleware import TenantMiddleware
from django_tenant_domains.models import Tenant
from tests import urls

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _skip_access_checks(monkeypatch):
    """This file tests how a tenant is found and scoped. tests/test_access.py tests access."""
    monkeypatch.setattr(TenantMiddleware, "_authorize", lambda self, request, tenant: Outcome())


@pytest.fixture
def acme():
    return Tenant.objects.create(slug="acme", name="Acme")


@pytest.fixture
def client():
    return Client(raise_request_exception=False)


def test_url_sets_the_context_and_the_request(client, acme):
    assert client.get("/t/acme/").json() == {"context": "acme", "request": "acme"}


def test_context_cleared_after_the_response(client, acme):
    client.get("/t/acme/")
    assert get_current_tenant() is None


def test_context_cleared_when_the_view_raises(client, acme):
    assert client.get("/t/acme/boom/").status_code == 500
    assert get_current_tenant() is None


def test_unknown_tenant_in_url_is_404(client):
    assert client.get("/t/nobody/").status_code == 404


def test_unknown_url_tenant_ignores_the_session(client, acme):
    client.get("/t/acme/")
    assert client.get("/t/nobody/").status_code == 404


def test_no_tenant_on_a_plain_url(client):
    assert client.get("/plain/").json() == {"context": None, "request": None}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/t/ACME/", "/t/acme/"),
        ("/t/AcMe/nested/page/", "/t/acme/nested/page/"),
        ("/t/%20acme%20/", "/t/acme/"),
        ("/t/ACME/?x=1", "/t/acme/?x=1"),
    ],
)
def test_non_canonical_slug_redirects(client, acme, path, expected):
    response = client.get(path)
    assert response.status_code == 301
    assert response["Location"] == expected


def test_redirect_changes_only_the_tenant_segment(client, acme):
    response = client.get("/t/ACME/sub/t/ACME/")
    assert response["Location"] == "/t/acme/sub/t/ACME/"


def test_redirect_keeps_the_method_and_body_for_a_post(client, acme):
    assert client.post("/t/ACME/").status_code == 308


def test_non_canonical_unknown_slug_is_404_not_a_redirect(client):
    assert client.get("/t/NOBODY/").status_code == 404


def test_session_remembers_the_last_tenant(client, acme):
    client.get("/t/acme/")
    assert client.get("/plain/").json() == {"context": "acme", "request": "acme"}


def test_deleted_tenant_in_session_is_forgotten(client, acme):
    client.get("/t/acme/")
    acme.delete()
    assert client.get("/plain/").json() == {"context": None, "request": None}
    assert "tenant_domains_last_tenant" not in client.session


def test_session_is_written_only_when_the_tenant_changes(acme):
    class Session(dict):
        writes = 0

        def __setitem__(self, key, value):
            type(self).writes += 1
            super().__setitem__(key, value)

    middleware = TenantMiddleware(lambda request: HttpResponse())
    request = RequestFactory().get("/t/acme/")
    request.session = Session()
    middleware(request)
    middleware(request)
    assert Session.writes == 1


def test_later_visit_replaces_the_remembered_tenant(client, acme):
    Tenant.objects.create(slug="other", name="Other")
    client.get("/t/acme/")
    client.get("/t/other/")
    assert client.get("/plain/").json()["context"] == "other"


@override_settings(TENANT_DOMAINS_RESOLVERS=["django_tenant_domains.resolvers.HeaderResolver"])
def test_resolver_chain_is_configurable(acme):
    client = Client()
    assert client.get("/plain/", headers={"x-tenant": "acme"}).json()["context"] == "acme"
    assert client.get("/t/acme/").json()["context"] is None


@pytest.mark.django_db(transaction=True)
def test_overlapping_async_requests_keep_their_own_tenant():
    Tenant.objects.create(slug="one", name="One")
    Tenant.objects.create(slug="two", name="Two")
    Tenant.objects.create(slug="three", name="Three")

    async def request(client, path):
        response = await client.get(path)
        # The middleware must have cleaned up in the task that handled the request.
        return response, get_current_tenant()

    async def main():
        urls.barrier = asyncio.Barrier(3)
        client = AsyncClient(raise_request_exception=False)
        return await asyncio.gather(
            request(client, "/t/one/async/"),
            request(client, "/t/two/async/"),
            request(client, "/t/three/async-boom/"),
        )

    (one, one_after), (two, two_after), (three, three_after) = async_to_sync(main)()
    assert one.json() == {"before": "one", "after": "one"}
    assert two.json() == {"before": "two", "after": "two"}
    assert three.status_code == 500
    assert (one_after, two_after, three_after) == (None, None, None)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/t/ACME/file/report%3Fdraft/", "/t/acme/file/report%3Fdraft/"),
        ("/t/ACME/file/a%23b/", "/t/acme/file/a%23b/"),
        ("/t/ACME/file/a%252Fb/", "/t/acme/file/a%252Fb/"),
        ("/x/ACME/extra/t/ACME/", "/x/ACME/extra/t/acme/"),
        ("/org-ACME/", "/org-acme/"),
    ],
)
def test_redirect_changes_only_the_tenant_argument(client, acme, path, expected):
    response = client.get(path)
    assert response.status_code == 301
    assert response["Location"] == expected


def test_streaming_body_runs_as_the_tenant(client, acme):
    response = client.get("/t/acme/stream/")
    assert b"".join(response.streaming_content) == b"acme,acme"
    assert get_current_tenant() is None


def test_streaming_generator_does_not_leave_the_tenant_set_between_chunks(client, acme):
    content = iter(client.get("/t/acme/stream/").streaming_content)
    assert next(content) == b"acme,"
    assert get_current_tenant() is None


@pytest.mark.django_db(transaction=True)
def test_async_streaming_body_runs_as_the_tenant():
    Tenant.objects.create(slug="acme", name="Acme")

    async def main():
        response = await AsyncClient().get("/t/acme/async-stream/")
        return [chunk async for chunk in response], get_current_tenant()

    chunks, after = async_to_sync(main)()
    assert b"".join(chunks) == b"acme,acme"
    assert after is None


def test_file_responses_keep_working(client, acme):
    response = client.get("/t/acme/download/")
    assert isinstance(response, FileResponse)
    assert b"".join(response.streaming_content) == b"file-bytes"


def _run(body_factory):
    """Run a request as `acme` with a body the view builds, and return the response."""
    middleware = TenantMiddleware(lambda request: body_factory())
    return middleware(RequestFactory().get("/t/acme/"))


def _slug():
    tenant = get_current_tenant()
    return tenant.slug if tenant else None


def test_tenant_context_inside_a_stream_survives_between_chunks(acme):
    other = Tenant(pk=999, slug="other", name="Other")

    def body():
        with tenant_context(other):
            yield _slug()
            yield _slug()
        yield _slug()

    response = _run(lambda: StreamingHttpResponse(body()))
    assert list(response.streaming_content) == [b"other", b"other", b"acme"]
    assert _slug() is None


def test_closing_a_stream_early_runs_its_cleanup_as_the_tenant(acme):
    seen = []

    def body():
        try:
            yield "first"
        finally:
            seen.append(_slug())

    response = _run(lambda: StreamingHttpResponse(body()))
    next(iter(response.streaming_content))
    response.close()
    assert seen == ["acme"]
    assert _slug() is None


def test_closing_a_stream_inside_tenant_context_does_not_leak(acme):
    other = Tenant(pk=999, slug="other", name="Other")

    def body():
        with tenant_context(other):
            yield "first"

    response = _run(lambda: StreamingHttpResponse(body()))
    next(iter(response.streaming_content))
    response.close()
    assert _slug() is None


def test_file_response_from_a_generator_runs_as_the_tenant(acme):
    def body():
        yield _slug().encode()

    response = _run(lambda: FileResponse(body()))
    assert b"".join(response.streaming_content) == b"acme"


def test_file_response_with_a_custom_read_runs_as_the_tenant(acme):
    class Reader:
        done = False

        def read(self, size=-1):
            if self.done:
                return b""
            self.done = True
            return _slug().encode()

    response = _run(lambda: FileResponse(Reader()))
    assert b"".join(response.streaming_content) == b"acme"


def test_file_response_with_an_in_memory_reader_runs_as_the_tenant(acme):
    class TenantFile(io.BytesIO):
        def read(self, size=-1):
            return super().read(size) and _slug().encode()

    response = _run(lambda: FileResponse(TenantFile(b"x")))
    # A WSGI server's file wrapper would read the file itself, with no tenant.
    assert response.file_to_stream is None
    assert b"".join(response.streaming_content) == b"acme"


def test_real_file_response_keeps_its_file_handling(acme, tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"bytes")
    response = _run(lambda: FileResponse(path.open("rb")))
    assert response.file_to_stream is not None
    assert b"".join(response.streaming_content) == b"bytes"
    response.close()


@pytest.mark.django_db(transaction=True)
def test_async_tenant_context_inside_a_stream_survives_between_chunks():
    Tenant.objects.create(slug="acme", name="Acme")
    other = Tenant(pk=999, slug="other", name="Other")

    async def body():
        with tenant_context(other):
            yield _slug()
            await asyncio.sleep(0)
            yield _slug()
        yield _slug()

    async def get_response(request):
        return StreamingHttpResponse(body())

    async def main():
        response = await TenantMiddleware(get_response)(RequestFactory().get("/t/acme/"))
        return [chunk async for chunk in response], _slug()

    chunks, after = async_to_sync(main)()
    assert chunks == [b"other", b"other", b"acme"]
    assert after is None
