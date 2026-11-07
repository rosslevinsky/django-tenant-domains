"""Try hard to read the other tenant's rows through the ORM."""

import pytest
from django.db.models import Count, Exists, Q, Subquery

from tests.support import leak_db
from tests.testapp.models import Project, Task

pytestmark = leak_db


def test_all(world):
    assert list(Project.objects.all()) == [world.mine.project]


def test_filter_by_their_name(world):
    assert not Project.objects.filter(name=world.theirs.project.name).exists()


def test_get_by_their_pk(world):
    with pytest.raises(Project.DoesNotExist):
        Project.objects.get(pk=world.theirs.project.pk)


def test_filter_by_their_tenant(world):
    assert not Project.objects.filter(tenant=world.theirs.tenant).exists()


def test_or_with_their_pk(world):
    rows = Project.objects.filter(Q(pk=world.theirs.project.pk) | Q(name="nothing"))
    assert list(rows) == []


def test_count(world):
    assert Project.objects.count() == 1


def test_values_and_values_list(world):
    assert list(Project.objects.values_list("name", flat=True)) == ["mine-project"]
    assert list(Project.objects.values("name")) == [{"name": "mine-project"}]


def test_first_last_and_latest(world):
    assert Project.objects.first() == world.mine.project
    assert Project.objects.last() == world.mine.project
    assert Project.objects.latest("pk") == world.mine.project


def test_in_bulk(world):
    found = Project.objects.in_bulk([world.mine.project.pk, world.theirs.project.pk])
    assert list(found) == [world.mine.project.pk]


def test_aggregate(world):
    assert Project.objects.aggregate(n=Count("pk")) == {"n": 1}


def test_annotate_across_a_relation(world):
    rows = Project.objects.annotate(n=Count("tasks")).values_list("name", "n")
    assert list(rows) == [("mine-project", 1)]


def test_iterator(world):
    assert list(Project.objects.iterator()) == [world.mine.project]


def test_reverse_related_manager(world):
    assert list(world.mine.project.tasks.all()) == [world.mine.task]


def test_reverse_manager_on_their_record_is_filtered_to_mine(world):
    assert list(world.theirs.project.tasks.all()) == []


def test_prefetch_related(world):
    project = Project.objects.prefetch_related("tasks").get()
    assert list(project.tasks.all()) == [world.mine.task]


def test_select_related_stays_in_tenant(world):
    task = Task.objects.select_related("project").get()
    assert task.project == world.mine.project


def test_filter_across_a_relation(world):
    assert not Task.objects.filter(project__name="theirs-project").exists()


def test_subquery_from_the_scoped_manager(world):
    # Not tied to the outer row, so only the inner tenant filter can hide their task.
    theirs = Task.objects.filter(title="theirs-task").values("title")[:1]
    assert Project.objects.annotate(title=Subquery(theirs)).get().title is None


def test_exists_subquery(world):
    theirs = Task.objects.filter(title="theirs-task")
    assert not Project.objects.filter(Exists(theirs)).exists()


def test_union_of_scoped_querysets(world):
    rows = Project.objects.filter(name="mine-project").order_by()
    rows = rows.union(Project.objects.order_by())
    assert list(rows) == [world.mine.project]


def test_get_or_create_does_not_return_theirs(world):
    project, created = Project.objects.get_or_create(name="theirs-project")
    assert created is True
    assert project.tenant == world.mine.tenant
    assert project != world.theirs.project
