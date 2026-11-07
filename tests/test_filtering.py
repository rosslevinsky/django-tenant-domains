import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import JsonResponse
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from django_tenant_domains.context import no_tenant, tenant_context
from django_tenant_domains.exceptions import TenantMismatch, TenantNotSet
from django_tenant_domains.middleware import TenantMiddleware
from django_tenant_domains.models import Membership, Role, Tenant, TenantModel
from tests.support import orm_only
from tests.testapp.models import CustomChild, Project, Tag, Task

pytestmark = [pytest.mark.django_db, orm_only]


@pytest.fixture
def tenants():
    a = Tenant.objects.create(slug="a", name="A")
    b = Tenant.objects.create(slug="b", name="B")
    with tenant_context(a):
        Project.objects.create(name="a-project")
    with tenant_context(b):
        Project.objects.create(name="b-project")
    return a, b


def test_no_tenant_set_raises(tenants):
    with pytest.raises(TenantNotSet):
        Project.objects.all()


@pytest.mark.parametrize(
    "operation",
    [
        lambda: Project.objects.count(),
        lambda: Project.objects.filter(name="x"),
        lambda: Project.objects.get(pk=1),
        lambda: Project.objects.create(name="x"),
    ],
)
def test_every_manager_route_raises_without_a_tenant(tenants, operation):
    with pytest.raises(TenantNotSet):
        operation()


def test_save_without_a_tenant_raises(tenants):
    with pytest.raises(TenantNotSet):
        Project(name="x", tenant=tenants[0]).save()


def test_delete_without_a_tenant_raises(tenants):
    project = Project.unscoped_objects.first()
    with pytest.raises(TenantNotSet):
        project.delete()


def test_no_tenant_sees_every_row(tenants):
    with no_tenant():
        assert Project.objects.count() == 2


def test_create_under_no_tenant_needs_an_explicit_tenant(tenants):
    with no_tenant(), pytest.raises(TenantNotSet):
        Project.objects.create(name="x")


def test_create_under_no_tenant_with_explicit_tenant(tenants):
    with no_tenant():
        assert Project.objects.create(name="x", tenant=tenants[1]).tenant == tenants[1]


def test_unscoped_objects_sees_every_row_without_a_tenant(tenants):
    assert Project.unscoped_objects.count() == 2


def test_tenant_context_selects_rows(tenants):
    with tenant_context(tenants[1]):
        assert list(Project.objects.values_list("name", flat=True)) == ["b-project"]


def test_objects_is_the_default_manager():
    assert Project._default_manager is Project.objects


def test_subclass_with_its_own_meta_keeps_the_guarded_base_manager():
    assert Tag._base_manager.name == "unscoped_objects"
    assert Tag._meta.base_manager_name == "unscoped_objects"


def test_related_object_uses_plain_base_manager(tenants):
    with tenant_context(tenants[0]):
        project = Project.objects.get()
        task = Task.objects.create(title="x", project=project)
    fresh = Task.unscoped_objects.get(pk=task.pk)
    assert fresh.project == project


def test_refresh_and_foreign_key_access_work_without_a_tenant(tenants):
    project = Project.unscoped_objects.first()
    project.refresh_from_db()
    assert project.tenant in tenants


def test_async_query_uses_the_tenant(tenants):
    async def main():
        with tenant_context(tenants[0]):
            return await Project.objects.acount()

    assert async_to_sync(main)() == 1


def test_tenant_model_declares_managers_in_order():
    assert [m.name for m in TenantModel._meta.managers] == ["objects", "unscoped_objects"]


def test_request_filters_through_the_middleware(tenants):
    def view(request, **kwargs):
        return JsonResponse({"names": list(Project.objects.values_list("name", flat=True))})

    user = get_user_model().objects.create_user("bo")
    Membership.objects.create(
        user=user, tenant=tenants[1], role=Role.objects.get(tenant=None, name="member")
    )
    request = RequestFactory().get("/t/b/")
    request.user = user
    response = TenantMiddleware(lambda request: view(request))(request)
    assert b"b-project" in response.content
    assert b"a-project" not in response.content


class TestDeferredFields:
    @pytest.fixture
    def task(self, tenants):
        with tenant_context(tenants[0]):
            yield Task.objects.create(title="t", project=Project.objects.get())

    def test_save_keeps_the_update_narrow(self, task):
        loaded = Task.objects.only("title").get(pk=task.pk)
        loaded.title = "changed"
        with CaptureQueriesContext(connection) as queries:
            loaded.save()
        updates = [q["sql"] for q in queries if q["sql"].startswith("UPDATE")]
        assert len(updates) == 1
        assert "tenant_id" not in updates[0]
        assert "project_id" not in updates[0]

    def test_deferred_fields_stay_deferred(self, task):
        loaded = Task.objects.only("title").get(pk=task.pk)
        loaded.save()
        assert "tenant_id" in loaded.get_deferred_fields()

    def test_changes_to_a_deferred_key_are_not_overwritten(self, task):
        other = Project.objects.create(name="other")
        loaded = Task.objects.only("title").get(pk=task.pk)
        Task.objects.filter(pk=task.pk).update(project=other)
        loaded.title = "changed"
        loaded.save()
        assert Task.objects.get(pk=task.pk).project == other

    def test_deferred_record_of_another_tenant_is_still_refused(self, tenants, task):
        loaded = Task.unscoped_objects.only("title").get(pk=task.pk)
        loaded.title = "stolen"
        with tenant_context(tenants[1]), pytest.raises(TenantMismatch):
            loaded.save()


class TestAdmin:
    @pytest.fixture
    def staff(self):
        client = Client()
        client.force_login(get_user_model().objects.create_superuser("root"))
        return client

    def test_changelist_shows_every_tenant(self, staff, tenants):
        response = staff.get("/admin/testapp/project/")
        assert response.status_code == 200
        assert b"a-project" in response.content
        assert b"b-project" in response.content

    def test_changelist_ignores_a_remembered_tenant(self, staff, tenants):
        staff.get("/t/a/")
        assert b"b-project" in staff.get("/admin/testapp/project/").content

    def test_change_form_opens_and_saves(self, staff, tenants):
        project = Project.unscoped_objects.get(name="b-project")
        url = f"/admin/testapp/project/{project.pk}/change/"
        assert staff.get(url).status_code == 200
        response = staff.post(url, {"name": "renamed", "tenant": project.tenant_id})
        assert response.status_code == 302
        assert Project.unscoped_objects.get(pk=project.pk).name == "renamed"

    def test_add_form_with_a_foreign_key(self, staff, tenants):
        project = Project.unscoped_objects.get(name="a-project")
        assert staff.get("/admin/testapp/task/add/").status_code == 200
        response = staff.post(
            "/admin/testapp/task/add/",
            {"title": "t", "tenant": project.tenant_id, "project": project.pk},
        )
        assert response.status_code == 302
        assert Task.unscoped_objects.filter(title="t").exists()

    def test_delete_view(self, staff, tenants):
        project = Project.unscoped_objects.get(name="a-project")
        url = f"/admin/testapp/project/{project.pk}/delete/"
        assert staff.get(url).status_code == 200
        assert staff.post(url, {"post": "yes"}).status_code == 302
        assert not Project.unscoped_objects.filter(pk=project.pk).exists()

    def test_autocomplete_works_across_tenants(self, staff, tenants):
        url = "/admin/autocomplete/?app_label=testapp&model_name=task&field_name=reviewer&term=b"
        response = staff.get(url)
        assert response.status_code == 200
        theirs = Project.unscoped_objects.get(name="b-project")
        assert [row["id"] for row in response.json()["results"]] == [str(theirs.pk)]

    def test_autocomplete_ignores_a_remembered_tenant(self, staff, tenants):
        staff.get("/t/a/")
        url = "/admin/autocomplete/?app_label=testapp&model_name=task&field_name=reviewer&term=b"
        theirs = Project.unscoped_objects.get(name="b-project")
        assert [row["id"] for row in staff.get(url).json()["results"]] == [str(theirs.pk)]

    def test_cross_tenant_choice_is_a_form_error(self, staff, tenants):
        theirs = Project.unscoped_objects.get(name="b-project")
        response = staff.post(
            "/admin/testapp/task/add/",
            {"title": "t", "tenant": tenants[0].pk, "project": theirs.pk},
        )
        assert response.status_code == 200
        assert not Task.unscoped_objects.filter(title="t").exists()


class TestClean:
    def test_clean_reports_a_cross_tenant_key(self, tenants):
        theirs = Project.unscoped_objects.get(name="b-project")
        with tenant_context(tenants[0]), pytest.raises(ValidationError):
            Task(title="t", tenant=tenants[0], project=theirs).clean()

    def test_clean_reports_another_tenant_as_owner(self, tenants):
        with tenant_context(tenants[0]), pytest.raises(ValidationError):
            Project(name="x", tenant=tenants[1]).clean()

    def test_clean_accepts_a_record_about_to_be_stamped(self, tenants):
        mine = Project.unscoped_objects.get(name="a-project")
        with tenant_context(tenants[0]):
            Task(title="t", project=mine).clean()


def test_unscoped_update_refuses_to_move_records_to_another_tenant(tenants):
    a, b = tenants
    with tenant_context(a):
        task = Task.objects.create(title="t", project=Project.objects.get())
        with pytest.raises(TenantMismatch):
            Task.unscoped_objects.filter(pk=task.pk).update(tenant=b)
    assert Task.unscoped_objects.get(pk=task.pk).tenant_id == a.pk


def test_unscoped_update_can_move_records_without_a_tenant(tenants):
    a, b = tenants
    with tenant_context(a):
        task = Task.objects.create(title="t", project=Project.objects.get())
    Task.unscoped_objects.filter(pk=task.pk).update(tenant=b)
    assert Task.unscoped_objects.get(pk=task.pk).tenant_id == b.pk


def test_inherited_child_with_its_own_key_checks_only_its_parent(tenants):
    a, b = tenants
    with tenant_context(b):
        Project.objects.create(pk=20, name="unrelated")
    with tenant_context(a):
        Project.objects.create(pk=10, name="parent")
        # The child's own key is 20, which in the parent table is B's unrelated project.
        CustomChild(child_id=20, project_ptr_id=10, tenant=a, name="child").save()
    assert Project.unscoped_objects.get(pk=10).tenant_id == a.pk


def test_many_to_many_add_refuses_links_on_another_tenants_record(tenants):
    a, b = tenants
    with tenant_context(b):
        project = Project.objects.get()
        tag = Tag.objects.create(name="b-tag")
    with tenant_context(a):
        # add() opens no savepoint, so its refusal would break the test's transaction.
        with pytest.raises(TenantMismatch), transaction.atomic():
            project.tags.add(tag.pk)
        with pytest.raises(TenantMismatch), transaction.atomic():
            tag.project_set.add(project)
    assert not Project.tags.through.objects.filter(project=project).exists()


def test_many_to_many_add_checks_targets_against_the_stored_owner(tenants):
    a, b = tenants
    with tenant_context(b):
        tag = Tag.objects.create(name="b-tag")
    with tenant_context(a):
        project = Project.objects.get()
        project.tenant_id = b.pk  # Forged, never saved.
        with pytest.raises(TenantMismatch), transaction.atomic():
            project.tags.add(tag.pk)
    assert not Project.tags.through.objects.filter(project=project).exists()


def test_many_to_many_add_under_no_tenant_checks_the_stored_owner(tenants):
    a, b = tenants
    with tenant_context(b):
        tag = Tag.objects.create(name="b-tag")
    with no_tenant():
        project = Project.objects.get(tenant=a)
        project.tenant_id = b.pk
        with pytest.raises(TenantMismatch):
            project.tags.add(tag.pk)
