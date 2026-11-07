"""Records what happens at each known gap, so a change in behavior is noticed.

Passing here does not mean the behavior is good. See docs/known-gaps.md.
"""

import pytest
from django import forms
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count

from django_tenant_domains.context import no_tenant, tenant_context
from django_tenant_domains.exceptions import TenantNotSet
from django_tenant_domains.models import Membership, Role, Tenant
from tests.support import orm_only
from tests.testapp.models import Comment, Project, Tag, Task

pytestmark = [pytest.mark.django_db, orm_only]


@pytest.fixture
def world():
    a = Tenant.objects.create(slug="a", name="A")
    b = Tenant.objects.create(slug="b", name="B")
    with tenant_context(a):
        a_project = Project.objects.create(name="a-project")
        Task.objects.create(title="a-task", project=a_project)
    with tenant_context(b):
        b_project = Project.objects.create(name="b-project")
        Task.objects.create(title="b-task", project=b_project)
        b_tag = Tag.objects.create(name="b-tag")
    return a, b, a_project, b_project, b_tag


def test_raw_query_through_the_manager_sees_every_tenant(world):
    a, *_ = world
    table = Project._meta.db_table
    with tenant_context(a):
        rows = list(Project.objects.raw(f"SELECT * FROM {table}"))
    assert len(rows) == 2


def test_cursor_sql_sees_every_tenant(world):
    a, *_ = world
    with tenant_context(a), connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {Project._meta.db_table}")
        assert cursor.fetchone()[0] == 2


def test_values_across_a_relation_stays_in_tenant(world):
    a, *_ = world
    with tenant_context(a):
        names = list(Task.objects.values_list("project__name", flat=True))
    assert names == ["a-project"]


def test_values_from_an_unscoped_model_into_a_scoped_one_sees_every_tenant(world):
    a, _, a_project, b_project, _ = world
    Comment.objects.create(project=a_project)
    Comment.objects.create(project=b_project)
    with tenant_context(a):
        names = set(Comment.objects.values_list("project__name", flat=True))
    assert names == {"a-project", "b-project"}


def test_aggregate_from_an_unscoped_model_sees_every_tenant(world):
    a, _, a_project, b_project, _ = world
    user = get_user_model().objects.create_user("ann")
    Comment.objects.create(project=a_project, author=user)
    Comment.objects.create(project=b_project, author=user)
    with tenant_context(a):
        row = get_user_model().objects.annotate(n=Count("comment__project")).get()
    assert row.n == 2


def test_foreign_key_access_loads_a_row_without_filtering(world):
    a, _, _, b_project, _ = world
    comment = Comment.objects.create(project=b_project)
    with tenant_context(a):
        assert Comment.objects.get(pk=comment.pk).project == b_project


def test_admin_needs_tenant_model_admin(world):
    """A plain ModelAdmin uses the default manager, so it raises with no tenant."""
    from django.contrib import admin

    model_admin = admin.ModelAdmin(Project, admin.site)
    with pytest.raises(TenantNotSet):
        model_admin.get_queryset(None)


def test_many_to_many_through_model_rows_are_not_checked(world):
    _, _, a_project, _, b_tag = world
    a_project.tags.through.objects.create(project=a_project, tag=b_tag)
    with tenant_context(world[0]):
        assert list(a_project.tags.all()) == []
        names = list(Project.objects.values_list("tags__name", flat=True))
    assert names == ["b-tag"]


def test_join_through_an_unscoped_table_reaches_another_tenant(world):
    a, _, a_project, b_project, _ = world
    user = get_user_model().objects.create_user("ann")
    Comment.objects.create(project=a_project, author=user)
    Comment.objects.create(project=b_project, author=user)
    with tenant_context(a):
        path = "comments__author__comment__project__name"
        names = set(Project.objects.values_list(path, flat=True))
    assert names == {"a-project", "b-project"}


def test_refresh_from_db_on_a_forged_instance_reads_another_tenant(world):
    a, _, _, b_project, _ = world
    with tenant_context(a):
        forged = Project(pk=b_project.pk)
        forged.refresh_from_db()
    assert forged.name == "b-project"


def test_unscoped_queryset_delete_reaches_another_tenant(world):
    a, _, _, b_project, _ = world
    Task.unscoped_objects.filter(project=b_project).delete()
    with tenant_context(a):
        Project.unscoped_objects.filter(pk=b_project.pk).delete()
    assert not Project.unscoped_objects.filter(pk=b_project.pk).exists()


def test_unscoped_bulk_create_reaches_another_tenant(world):
    a, b, *_ = world
    with tenant_context(a):
        Project.unscoped_objects.bulk_create([Project(name="planted", tenant=b)])
    assert Project.unscoped_objects.get(name="planted").tenant == b


def test_cascade_can_delete_another_tenants_row_with_a_cross_tenant_key(world):
    a, _, a_project, _, _ = world
    stray = Task.unscoped_objects.get(title="b-task")
    Task.unscoped_objects.filter(pk=stray.pk).update(project=a_project)
    with tenant_context(a):
        a_project.delete()
    assert not Task.unscoped_objects.filter(pk=stray.pk).exists()


def test_queryset_update_under_no_tenant_does_not_check_foreign_keys(world):
    _, _, _, b_project, _ = world
    with no_tenant():
        Task.objects.filter(title="a-task").update(project=b_project)
    assert Task.unscoped_objects.get(title="a-task").project == b_project


def test_queryset_built_at_import_time_raises(world):
    with pytest.raises(TenantNotSet):
        Project.objects.all()


def test_queryset_keeps_the_tenant_it_was_built_under(world):
    a, b, *_ = world
    with tenant_context(a):
        rows = Project.objects.all()
    with tenant_context(b):
        assert [row.name for row in rows] == ["a-project"]


def test_model_form_with_a_scoped_foreign_key_raises_at_definition(world):
    with pytest.raises(TenantNotSet):

        class TaskForm(forms.ModelForm):
            class Meta:
                model = Task
                fields = ["title", "project"]


def test_model_form_workaround_sets_the_queryset_per_request(world):
    a, _, a_project, b_project, _ = world

    class TaskForm(forms.ModelForm):
        project = forms.ModelChoiceField(queryset=Project.unscoped_objects.none())

        class Meta:
            model = Task
            fields = ["title", "project"]

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fields["project"].queryset = Project.objects.all()

    with tenant_context(a):
        assert TaskForm({"title": "x", "project": a_project.pk}).is_valid()
        assert not TaskForm({"title": "x", "project": b_project.pk}).is_valid()


def test_bulk_create_can_skip_the_membership_role_check(world):
    a, b, *_ = world
    user = get_user_model().objects.create_user("ann")
    foreign_role = Role.objects.create(tenant=b, name="auditor")
    Membership.objects.bulk_create([Membership(user=user, tenant=a, role=foreign_role)])
    assert Membership.objects.get(user=user).role == foreign_role
