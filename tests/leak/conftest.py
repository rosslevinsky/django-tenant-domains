from dataclasses import dataclass

import pytest
from django.db import connection

from django_tenant_domains.context import _State, _switch, tenant_context
from django_tenant_domains.models import Tenant
from django_tenant_domains.rls import SETTING_NAME
from tests.support import POSTGRES
from tests.testapp.models import Project, Tag, Task


@dataclass
class Side:
    tenant: Tenant
    project: Project
    task: Task
    tag: Tag


@dataclass
class World:
    mine: Side
    theirs: Side


def _side(slug):
    tenant = Tenant.objects.create(slug=slug, name=slug)
    with tenant_context(tenant):
        project = Project.objects.create(name=f"{slug}-project")
        task = Task.objects.create(title=f"{slug}-task", project=project)
        tag = Tag.objects.create(name=f"{slug}-tag")
    return Side(tenant, project, task, tag)


def _session_setting(value):
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, false)", [SETTING_NAME, value])


@pytest.fixture
def world():
    """Two tenants with the same shape of data. The test runs as `mine`."""
    built = World(_side("mine"), _side("theirs"))
    if not POSTGRES:
        with tenant_context(built.mine.tenant):
            yield built
        return
    # tenant_context would hold one transaction open for the whole test, and the bypass login
    # could not see an attack's uncommitted write. A session setting lets each write commit.
    _session_setting(str(built.mine.tenant.pk))
    previous = _switch(_State(tenant=built.mine.tenant))
    try:
        yield built
    finally:
        _switch(previous)
        _session_setting("")
