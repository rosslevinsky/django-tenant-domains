"""Real requests as the ordinary role, without a wrapping test transaction. Postgres only."""

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import AsyncClient, Client, override_settings

from django_tenant_domains.access import Outcome
from django_tenant_domains.context import tenant_context
from django_tenant_domains.exceptions import TenantAsyncViewError
from django_tenant_domains.middleware import TenantMiddleware
from django_tenant_domains.models import Tenant
from tests import rls_urls
from tests.support import ALL_DATABASES, peek
from tests.testapp.models import Project, Tag, Task

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.django_db(transaction=True, databases=ALL_DATABASES),
]


@pytest.fixture(autouse=True)
def _skip_access_checks(monkeypatch):
    """This file tests how a tenant is found and scoped. tests/test_access.py tests access."""
    monkeypatch.setattr(TenantMiddleware, "_authorize", lambda self, request, tenant: Outcome())


@pytest.fixture(autouse=True)
def _urls():
    with override_settings(ROOT_URLCONF="tests.rls_urls"):
        yield


@pytest.fixture
def two():
    a = Tenant.objects.create(slug="a", name="A")
    b = Tenant.objects.create(slug="b", name="B")
    for tenant in (a, b):
        with tenant_context(tenant):
            Project.objects.create(name=f"{tenant.slug}-project")
    return a, b


@pytest.fixture
def client():
    return Client(raise_request_exception=False)


def test_request_reads_its_tenant_through_raw_sql_and_orm(client, two):
    assert client.get("/t/a/read/").json() == {"orm": ["a-project"], "raw": ["a-project"]}


def test_request_write_commits(client, two):
    assert client.post("/t/a/write/").status_code == 200
    assert peek(Tag).filter(name="written").exists()


def test_error_rolls_the_request_back(client, two):
    assert client.post("/t/a/write-then-boom/").status_code == 500
    assert not peek(Tag).filter(name="written").exists()


def test_http_404_from_a_view_rolls_back(client, two):
    assert client.post("/t/a/write-then-404/").status_code == 404
    assert not peek(Tag).filter(name="written").exists()


def test_next_request_on_the_same_connection_starts_clean(client, two):
    client.get("/t/a/read/")
    client.post("/t/a/write-then-boom/")
    assert client.get("/t/b/read/").json() == {"orm": ["b-project"], "raw": ["b-project"]}
    # A new client, since this one's session would resolve to tenant b again.
    assert Client().get("/plain-raw/").json() == {"raw": []}


def test_template_rendering_runs_in_the_transaction(client, two):
    assert client.get("/t/a/template/").content.strip() == b"a-project"


def test_streaming_body_can_query_as_the_tenant(client, two):
    response = client.get("/t/a/stream/")
    assert b"".join(response.streaming_content) == b"a-project,a-project"


def test_plain_async_view_is_refused():
    Tenant.objects.create(slug="a", name="A")

    async def main():
        return await AsyncClient().get("/t/a/async-plain/")

    with pytest.raises(TenantAsyncViewError):
        async_to_sync(main)()


def test_plain_async_view_without_a_tenant_still_works():
    async def main():
        return await AsyncClient().get("/async-no-tenant/")

    assert async_to_sync(main)().status_code == 200


def test_opted_in_async_view_reads_through_the_helper(two):
    async def main():
        return await AsyncClient().get("/t/a/async-helper/")

    assert async_to_sync(main)().json() == {"names": ["a-project"]}


def test_opted_in_async_view_outside_the_helper_finds_nothing(two):
    async def main():
        return await AsyncClient().get("/t/a/async-no-helper/")

    assert async_to_sync(main)().json() == {"count": 0}


def test_helper_writes_commit_and_errors_roll_back(two):
    async def main():
        ok = await AsyncClient().post("/t/a/async-write/")
        bad = await AsyncClient(raise_request_exception=False).post("/t/a/async-write-boom/")
        return ok, bad

    ok, bad = async_to_sync(main)()
    assert ok.status_code == 200
    assert bad.status_code == 500
    assert peek(Tag).filter(name="async-kept").exists()
    assert not peek(Tag).filter(name="async-gone").exists()


def test_sync_view_on_the_async_stack_uses_the_request_transaction(two):
    async def main():
        return await AsyncClient().get("/t/b/read/")

    assert async_to_sync(main)().json() == {"orm": ["b-project"], "raw": ["b-project"]}


def test_on_commit_callback_runs_as_the_tenant_of_the_request(client, two):
    rls_urls.on_commit_results.clear()
    assert client.get("/t/a/commit-hook/").status_code == 200
    assert rls_urls.on_commit_results == [(["a-project"], ["a-project"])]


def test_on_commit_callback_does_not_leak_the_tenant_afterward(client, two):
    client.get("/t/a/commit-hook/")
    assert Client().get("/plain-raw/").json() == {"raw": []}


class TestAdmin:
    @pytest.fixture
    def staff(self):
        client = Client()
        client.force_login(get_user_model().objects.create_superuser("root"))
        return client

    def test_changelist_shows_every_tenant(self, staff, two):
        response = staff.get("/admin/testapp/project/")
        assert b"a-project" in response.content
        assert b"b-project" in response.content

    def test_change_form_saves(self, staff, two):
        project = peek(Project).get(name="b-project")
        url = f"/admin/testapp/project/{project.pk}/change/"
        assert staff.get(url).status_code == 200
        response = staff.post(url, {"name": "renamed", "tenant": project.tenant_id})
        assert response.status_code == 302
        assert peek(Project).get(pk=project.pk).name == "renamed"

    def test_add_form_validates_the_foreign_key(self, staff, two):
        project = peek(Project).get(name="a-project")
        response = staff.post(
            "/admin/testapp/task/add/",
            {"title": "t", "tenant": project.tenant_id, "project": project.pk},
        )
        assert response.status_code == 302
        assert peek(Task).filter(title="t").exists()

    def test_cross_tenant_choice_is_a_form_error(self, staff, two):
        theirs = peek(Project).get(name="b-project")
        response = staff.post(
            "/admin/testapp/task/add/",
            {"title": "t", "tenant": two[0].pk, "project": theirs.pk},
        )
        assert response.status_code == 200
        assert not peek(Task).filter(title="t").exists()

    def test_delete_view(self, staff, two):
        project = peek(Project).get(name="a-project")
        url = f"/admin/testapp/project/{project.pk}/delete/"
        assert staff.post(url, {"post": "yes"}).status_code == 302
        assert not peek(Project).filter(pk=project.pk).exists()

    def test_autocomplete(self, staff, two):
        url = "/admin/autocomplete/?app_label=testapp&model_name=task&field_name=reviewer&term=b"
        response = staff.get(url)
        assert response.status_code == 200
        theirs = peek(Project).get(name="b-project")
        assert [row["id"] for row in response.json()["results"]] == [str(theirs.pk)]
