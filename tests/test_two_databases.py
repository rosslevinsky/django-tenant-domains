import os
import subprocess
import sys
from pathlib import Path

import django
import pytest

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = """
import django; django.setup()
from django.core.management import call_command
from django_tenant_domains.context import tenant_context
from django_tenant_domains.exceptions import TenantMismatch
from django_tenant_domains.models import Tenant
from tests.testapp.models import Project, Task

for alias in ("default", "other"):
    call_command("migrate", database=alias, verbosity=0)
a = Tenant.objects.using("default").create(slug="a", name="A")
b = Tenant.objects.using("other").create(pk=a.pk + 1, slug="b", name="B")
Project.unscoped_objects.using("default").bulk_create([Project(pk=7, name="a7", tenant_id=a.pk)])
Project.unscoped_objects.using("other").bulk_create([Project(pk=7, name="b7", tenant_id=b.pk)])

with tenant_context(a):
    # Project 8 belongs to A in default but B in other, so only the checked database matters.
    Project.unscoped_objects.using("default").bulk_create(
        [Project(pk=8, name="a8", tenant_id=a.pk)]
    )
    Project.unscoped_objects.using("other").bulk_create(
        [Project(pk=8, name="b8", tenant_id=b.pk)]
    )
    Task.unscoped_objects.using("default").bulk_create(
        [Task(pk=1, title="t", tenant_id=a.pk, project_id=8)]
    )
    mine = Project.objects.using("default").get(pk=7)
    for attempt in ATTEMPTS:
        try:
            attempt(mine)
        except TenantMismatch:
            pass
        else:
            raise SystemExit("write to the other database was not refused")
stored = Project.unscoped_objects.using("other").get(pk=7)
assert (stored.name, stored.tenant_id) == ("b7", b.pk)
"""


def _run(tmp_path, attempts, router=""):
    env = {
        "DTD_ROUTER": router,
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.settings_two_dbs",
        "DTD_DEFAULT_DB": str(tmp_path / "default.sqlite3"),
        "DTD_OTHER_DB": str(tmp_path / "other.sqlite3"),
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, "-c", f"ATTEMPTS = {attempts}\n{SCRIPT}"],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_keyword_and_positional_delete_check_the_database_written(tmp_path):
    attempts = "[lambda p: p.save(using='other'), lambda p: p.delete('other')]"
    result = _run(tmp_path, attempts)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(django.VERSION >= (6, 0), reason="Django 6.0 removed positional save()")
def test_positional_save_checks_the_database_written(tmp_path):
    result = _run(tmp_path, "[lambda p: p.save(False, False, 'other')]")
    assert result.returncode == 0, result.stdout + result.stderr


def test_deferred_save_to_another_database_checks_every_field_it_writes(tmp_path):
    attempts = (
        "[lambda p: Task.objects.using('default').only('title').get(title='t').save(using='other')]"
    )
    result = _run(tmp_path, attempts)
    assert result.returncode == 0, result.stdout + result.stderr


def test_queryset_writes_check_keys_in_the_database_they_write(tmp_path):
    env_router = "tests.settings_two_dbs.SplitRouter"
    attempts = (
        "[lambda p: Task.objects.update(project_id=8),"
        " lambda p: Task.objects.bulk_create([Task(title='x', project_id=8)]),"
        " lambda p: Task.objects.bulk_update([Task(pk=1, title='t', project_id=8)], ['project'])]"
    )
    result = _run(tmp_path, attempts, router=env_router)
    assert result.returncode == 0, result.stdout + result.stderr


SETUP = """
import django; django.setup()
from django.core.management import call_command
from django_tenant_domains.context import no_tenant, tenant_context
from django_tenant_domains.exceptions import TenantMismatch

for alias in ("default", "other"):
    call_command("migrate", database=alias, verbosity=0)
"""


def _run_script(tmp_path, script, router=""):
    """Run a script of its own on two databases that reuse the same keys for different rows."""
    env = {
        **os.environ,
        "DTD_ROUTER": router,
        "DJANGO_SETTINGS_MODULE": "tests.settings_two_dbs",
        "DTD_DEFAULT_DB": str(tmp_path / "default.sqlite3"),
        "DTD_OTHER_DB": str(tmp_path / "other.sqlite3"),
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", SETUP + script],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_reverse_update_checks_keys_on_the_database_it_writes(tmp_path):
    script = """
from django_tenant_domains.models import Tenant
from tests.testapp.models import Project, Task

for alias in ("default", "other"):
    Tenant.objects.using(alias).create(pk=1, slug="a", name="A")
    Tenant.objects.using(alias).create(pk=2, slug="b", name="B")
# Project 9 belongs to A in default but to B in other, where the update is written.
Project.unscoped_objects.using("default").bulk_create(
    [Project(pk=8, name="a8", tenant_id=1), Project(pk=9, name="a9", tenant_id=1)]
)
Project.unscoped_objects.using("other").bulk_create(
    [Project(pk=8, name="a8", tenant_id=1), Project(pk=9, name="b9", tenant_id=2)]
)
Task.unscoped_objects.using("other").bulk_create([Task(pk=1, title="t", tenant_id=1, project_id=8)])
with tenant_context(Tenant.objects.get(pk=1)):
    parent = Project.objects.using("other").get(pk=8)
    try:
        parent.tasks.update(reviewer_id=9)
    except TenantMismatch:
        pass
assert Task.unscoped_objects.using("other").get(pk=1).reviewer_id is None
"""
    _run_script(tmp_path, script)


def test_object_permission_reads_grants_from_the_objects_database(tmp_path):
    script = """
from django_tenant_domains.models import Tenant
from django_tenant_domains.sharing import READ, object_allows
from tests.testapp.models import Album, AlbumShare

for alias in ("default", "other"):
    Tenant.objects.using(alias).create(pk=1, slug="a", name="A")
    Tenant.objects.using(alias).create(pk=2, slug="b", name="B")
with no_tenant():
    # A owns album 5 in default. In other, album 5 is B's and only shared read-only with A.
    Album.objects.using("default").bulk_create([Album(pk=5, name="a5", tenant_id=1)])
    Album.objects.using("other").bulk_create([Album(pk=5, name="b5", tenant_id=2)])
    AlbumShare.objects.using("other").bulk_create(
        [AlbumShare(record_id=5, owner_id=2, grantee_id=1, access=READ)]
    )
a = Tenant.objects.get(pk=1)
album = Album.unscoped_objects.using("other").get(pk=5)
assert object_allows("testapp.view_album", album, a)
assert not object_allows("testapp.change_album", album, a)
"""
    _run_script(tmp_path, script)


def test_deleting_a_share_checks_the_owner_in_the_database_it_deletes_from(tmp_path):
    script = """
from django_tenant_domains.models import Tenant
from tests.testapp.models import Folder, FolderShare

for alias in ("default", "other"):
    for pk, slug in ((1, "a"), (2, "b"), (3, "c")):
        Tenant.objects.using(alias).create(pk=pk, slug=slug, name=slug)
with no_tenant():
    # Share 1 is A's in default, but in other it is a share from B to C.
    Folder.objects.using("default").bulk_create([Folder(pk=1, name="a1", tenant_id=1)])
    Folder.objects.using("other").bulk_create([Folder(pk=1, name="b1", tenant_id=2)])
    FolderShare.objects.using("default").bulk_create(
        [FolderShare(pk=1, record_id=1, owner_id=1, grantee_id=2)]
    )
    FolderShare.objects.using("other").bulk_create(
        [FolderShare(pk=1, record_id=1, owner_id=2, grantee_id=3)]
    )
theirs = FolderShare.unscoped_objects.using("other").get(pk=1)
with tenant_context(Tenant.objects.get(pk=1)):
    try:
        theirs.delete()
    except TenantMismatch:
        pass
assert FolderShare.unscoped_objects.using("other").filter(pk=1).exists()
"""
    _run_script(tmp_path, script)


def test_unscoped_update_checks_ownership_on_the_database_it_writes(tmp_path):
    script = """
from django_tenant_domains.models import Tenant
from tests.testapp.models import Project

for alias in ("default", "other"):
    Tenant.objects.using(alias).create(pk=1, slug="mine", name="Mine")
    Tenant.objects.using(alias).create(pk=2, slug="theirs", name="Theirs")
# Their project is only on the database written to, as on a primary ahead of its replica.
with no_tenant():
    Project.objects.using("other").create(pk=7, tenant_id=2, name="private")
with tenant_context(Tenant.objects.using("default").get(pk=1)):
    try:
        Project.unscoped_objects.filter(pk=7).update(name="changed")
    except TenantMismatch:
        pass
assert Project.unscoped_objects.using("other").get(pk=7).name == "private"
"""
    _run_script(tmp_path, script, router="tests.settings_two_dbs.SplitRouter")


def test_role_move_checks_memberships_on_the_database_it_writes(tmp_path):
    script = """
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django_tenant_domains.models import Membership, Role, Tenant

for alias in ("default", "other"):
    Tenant.objects.using(alias).create(pk=1, slug="a", name="A")
    Tenant.objects.using(alias).create(pk=2, slug="b", name="B")
    Role.objects.using(alias).create(pk=50, tenant_id=1, name="custom")
# Only the database written to has a member of A using the role.
user = get_user_model().objects.using("other").create(username="member")
Membership.objects.using("other").create(user_id=user.pk, tenant_id=1, role_id=50)
role = Role.objects.using("default").get(pk=50)
role.tenant_id = 2
try:
    role.save(using="other")
except ValidationError:
    pass
assert Role.objects.using("other").get(pk=50).tenant_id == 1
"""
    _run_script(tmp_path, script)
