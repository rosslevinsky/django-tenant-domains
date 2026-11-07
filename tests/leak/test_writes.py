"""Try hard to change the other tenant's rows through the ORM."""

import pytest

from django_tenant_domains.context import no_tenant
from tests.support import BYPASS, held, leak_db, no_effect_or_refused, peek, refused
from tests.testapp.models import CustomChild, Project, SpecialProject, Tag, Task

pytestmark = leak_db


def _unchanged(side):
    stored = peek(Project).get(pk=side.project.pk)
    return stored.name == side.project.name and stored.tenant_id == side.tenant.pk


def test_create_stamps_the_current_tenant(world):
    assert Project.objects.create(name="new").tenant == world.mine.tenant


def test_save_stamps_the_current_tenant(world):
    project = Project(name="new")
    project.save()
    assert project.tenant == world.mine.tenant


def test_create_with_their_tenant_is_refused(world):
    with refused():
        Project.objects.create(name="new", tenant=world.theirs.tenant)


def test_saving_their_record_is_refused(world):
    project = world.theirs.project
    project.name = "stolen"
    with refused():
        project.save()
    assert peek(Project).get(pk=project.pk).name == "theirs-project"


def test_taking_their_record_by_changing_its_tenant_is_refused(world):
    project = held(Project, world.theirs.project.pk)
    project.tenant = world.mine.tenant
    with refused():
        project.save()
    assert peek(Project).get(pk=project.pk).tenant == world.theirs.tenant


def test_overwriting_their_row_with_a_forged_pk_is_refused(world):
    forged = Project(pk=world.theirs.project.pk, name="stolen")
    with refused():
        forged.save()
    assert _unchanged(world.theirs)


def test_partial_save_of_their_record_is_refused(world):
    project = peek(Project).get(pk=world.theirs.project.pk)
    project.name = "stolen"
    with refused():
        project.save(update_fields=["name"])
    assert _unchanged(world.theirs)


def test_partial_save_with_forged_tenant_attribute_is_refused(world):
    project = held(Project, world.theirs.project.pk)
    project.tenant = world.mine.tenant
    project.name = "stolen"
    with refused():
        project.save(update_fields=["name"])
    assert _unchanged(world.theirs)


def test_giving_my_record_away_is_refused(world):
    project = world.mine.project
    project.tenant = world.theirs.tenant
    with refused():
        project.save()


def test_queryset_update_cannot_reach_their_rows(world):
    assert Project.objects.update(name="changed") == 1
    assert _unchanged(world.theirs)


@pytest.mark.parametrize("field", ["tenant", "tenant_id"])
def test_queryset_update_cannot_give_rows_away(world, field):
    value = world.theirs.tenant if field == "tenant" else world.theirs.tenant.pk
    with refused():
        Project.objects.update(**{field: value})


def test_queryset_delete_cannot_reach_their_rows(world):
    Task.objects.all().delete()
    assert peek(Task).filter(pk=world.theirs.task.pk).exists()
    assert not Task.objects.filter(pk=world.mine.task.pk).exists()


def test_deleting_their_record_is_refused(world):
    pk = world.theirs.task.pk
    with no_effect_or_refused():
        world.theirs.task.delete()
    assert peek(Task).filter(pk=pk).exists()


def test_deleting_with_a_forged_tenant_attribute_is_refused(world):
    task = held(Task, world.theirs.task.pk)
    task.tenant = world.mine.tenant
    with no_effect_or_refused():
        task.delete()
    assert peek(Task).filter(pk=world.theirs.task.pk).exists()


def test_update_or_create_does_not_touch_theirs(world):
    _, created = Project.objects.update_or_create(
        name="theirs-project", defaults={"name": "changed"}
    )
    assert created is True
    assert _unchanged(world.theirs)


def test_bulk_create_stamps_the_current_tenant(world):
    created = Project.objects.bulk_create([Project(name="bulk")])
    assert created[0].tenant == world.mine.tenant


def test_bulk_create_with_their_tenant_is_refused(world):
    with refused():
        Project.objects.bulk_create([Project(name="bulk", tenant=world.theirs.tenant)])


def test_bulk_update_cannot_reach_their_rows(world):
    theirs = peek(Project).get(pk=world.theirs.project.pk)
    theirs.name = "stolen"
    Project.objects.bulk_update([theirs], ["name"])
    assert _unchanged(world.theirs)


def test_bulk_update_cannot_give_rows_away(world):
    mine = world.mine.project
    mine.tenant = world.theirs.tenant
    with refused():
        Project.objects.bulk_update([mine], ["tenant"])


def test_foreign_key_to_their_record_is_refused(world):
    with refused():
        Task.objects.create(title="x", project=world.theirs.project)


def test_second_foreign_key_to_their_record_is_refused(world):
    task = world.mine.task
    task.reviewer = world.theirs.project
    with refused():
        task.save()


def test_foreign_key_check_reads_the_database_not_a_stale_object(world):
    stale = held(Project, world.theirs.project.pk)
    stale.tenant = world.mine.tenant
    with refused():
        Task.objects.create(title="x", project=stale)


def test_partial_save_checks_a_foreign_key_it_writes(world):
    task = world.mine.task
    task.project = world.theirs.project
    with refused():
        task.save(update_fields=["project"])


def test_foreign_key_is_checked_even_under_no_tenant(world):
    with no_tenant(), refused():
        Task.objects.create(title="x", tenant=world.mine.tenant, project=world.theirs.project)


def test_no_tenant_can_move_a_record_explicitly(world):
    # Under row-level security this also needs the bypass login.
    tag = peek(Tag).get(pk=world.mine.tag.pk)
    with no_tenant():
        tag.tenant_id = world.theirs.tenant.pk
        tag.save(using=BYPASS)
    assert peek(Tag).get(pk=tag.pk).tenant == world.theirs.tenant


def test_reverse_add_cannot_take_their_row(world):
    with no_effect_or_refused():
        world.mine.project.tasks.add(world.theirs.task)
    assert peek(Task).get(pk=world.theirs.task.pk).project == world.theirs.project


def test_reverse_add_without_bulk_cannot_take_their_row(world):
    with refused():
        world.mine.project.tasks.add(world.theirs.task, bulk=False)


def test_reverse_set_cannot_take_their_row(world):
    with no_effect_or_refused():
        world.mine.project.tasks.set([world.theirs.task])
    assert peek(Task).get(pk=world.theirs.task.pk).project == world.theirs.project


def test_reverse_add_of_my_own_row_works(world):
    other = Project.objects.create(name="other")
    other.tasks.add(world.mine.task)
    assert Task.objects.get(pk=world.mine.task.pk).project == other


def test_forged_child_cannot_overwrite_their_parent_row(world):
    forged = SpecialProject(project_ptr_id=world.theirs.project.pk, name="stolen")
    with refused():
        forged.save()
    assert _unchanged(world.theirs)


def test_forged_child_cannot_delete_their_parent_row(world):
    mine = SpecialProject.objects.create(name="mine-special")
    forged = SpecialProject(project_ptr_id=mine.pk, id=world.theirs.project.pk)
    with no_effect_or_refused():
        forged.delete()
    assert _unchanged(world.theirs)


def test_real_child_rows_save_and_delete(world):
    special = SpecialProject.objects.create(name="mine-special", level=1)
    assert special.tenant == world.mine.tenant
    special.level = 2
    special.save()
    assert SpecialProject.objects.get(pk=special.pk).level == 2
    special.delete()
    assert not Project.objects.filter(pk=special.pk).exists()


def test_positional_arguments_cannot_skip_the_checks(world):
    project = peek(Project).get(pk=world.theirs.project.pk)
    project.name = "stolen"
    with no_effect_or_refused():
        project.delete("default")
    assert _unchanged(world.theirs)


def test_queryset_update_cannot_point_a_key_at_their_record(world):
    with refused():
        Task.objects.update(project=world.theirs.project)
    with refused():
        Task.objects.update(project_id=world.theirs.project.pk)


def test_queryset_update_can_point_a_key_at_my_record(world):
    other = Project.objects.create(name="other")
    Task.objects.update(project=other, reviewer=None)
    assert Task.objects.get().project == other


def test_bulk_create_cannot_point_a_key_at_their_record(world):
    with refused():
        Task.objects.bulk_create([Task(title="x", project=world.theirs.project)])


def test_bulk_update_cannot_point_a_key_at_their_record(world):
    task = world.mine.task
    task.project = world.theirs.project
    with refused():
        Task.objects.bulk_update([task], ["project"])


def test_forged_child_with_its_own_key_cannot_overwrite_their_parent_row(world):
    forged = CustomChild(child_id=99, project_ptr_id=world.theirs.project.pk, name="stolen")
    with refused():
        forged.save()
    assert _unchanged(world.theirs)


def test_reverse_add_cannot_attach_my_row_to_their_parent(world):
    theirs = held(Project, world.theirs.project.pk)
    with refused():
        theirs.tasks.add(world.mine.task)
    assert Task.objects.get(pk=world.mine.task.pk).project == world.mine.project


def test_many_to_many_add_of_their_record_is_refused(world):
    # add() runs in its own transaction, and the refusal rolls it back.
    with refused():
        world.mine.project.tags.add(world.theirs.tag)
    assert not world.mine.project.tags.exists()


def test_many_to_many_add_from_the_reverse_side_is_refused(world):
    with refused():
        world.mine.tag.project_set.add(world.theirs.project)


def test_many_to_many_set_of_their_record_is_refused(world):
    with refused():
        world.mine.project.tags.set([world.theirs.tag])


def test_many_to_many_add_of_my_record_works(world):
    world.mine.project.tags.add(world.mine.tag)
    assert list(world.mine.project.tags.all()) == [world.mine.tag]
