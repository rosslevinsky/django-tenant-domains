import asyncio

import pytest
from asgiref.sync import async_to_sync, sync_to_async

from django_tenant_domains.context import (
    get_current_tenant,
    is_tenant_bypassed,
    no_tenant,
    tenant_context,
)
from django_tenant_domains.models import Tenant

pytestmark = pytest.mark.django_db

A = Tenant(pk=1, slug="a", name="A")
B = Tenant(pk=2, slug="b", name="B")


def test_nothing_set_by_default():
    assert get_current_tenant() is None
    assert is_tenant_bypassed() is False


def test_tenant_context_sets_and_restores():
    with tenant_context(A):
        assert get_current_tenant() is A
    assert get_current_tenant() is None


def test_nested_contexts_restore_the_outer_one():
    with tenant_context(A):
        with tenant_context(B):
            assert get_current_tenant() is B
        assert get_current_tenant() is A


def test_no_tenant_bypasses_and_restores():
    with tenant_context(A):
        with no_tenant():
            assert get_current_tenant() is None
            assert is_tenant_bypassed() is True
        assert get_current_tenant() is A
        assert is_tenant_bypassed() is False
    assert is_tenant_bypassed() is False


def test_tenant_context_inside_no_tenant_ends_the_bypass():
    with no_tenant(), tenant_context(A):
        assert get_current_tenant() is A
        assert is_tenant_bypassed() is False


def test_context_restored_after_an_exception():
    with pytest.raises(ValueError), tenant_context(A):
        raise ValueError
    assert get_current_tenant() is None


def test_tenant_context_refuses_none():
    with pytest.raises(ValueError), tenant_context(None):
        pass


def test_concurrent_tasks_do_not_see_each_others_tenant():
    async def main():
        barrier = asyncio.Barrier(2)

        async def worker(tenant):
            with tenant_context(tenant):
                await barrier.wait()
                await asyncio.sleep(0)
                return get_current_tenant()

        return await asyncio.gather(worker(A), worker(B))

    assert asyncio.run(main()) == [A, B]


def test_context_reaches_sync_code_called_from_async():
    async def main():
        with tenant_context(A):
            return await sync_to_async(get_current_tenant)()

    assert async_to_sync(main)() is A
