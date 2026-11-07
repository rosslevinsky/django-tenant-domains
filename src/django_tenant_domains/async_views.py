"""How an async view opts in to working with row-level security."""

from asgiref.sync import sync_to_async

from .context import _state, _use


def tenant_async_view(view):
    """Allow this async view to handle a tenant's request while row-level security is on."""
    view._tenant_async_view = True
    return view


def is_tenant_async_view(view) -> bool:
    return getattr(view, "_tenant_async_view", False)


async def in_tenant_transaction(func, *args, **kwargs):
    """Run func as sync code, in one transaction, with the current tenant set in the database.

    Two calls are two transactions, so work that must succeed or fail together belongs in one
    function. Queries outside this helper have no tenant and return no rows.
    """
    state = _state.get()

    def run():
        with _use(state):
            return func(*args, **kwargs)

    return await sync_to_async(run)()
