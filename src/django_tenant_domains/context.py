"""The current tenant, held in a contextvar so that it is safe under async."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .rls import database_scope


@dataclass(frozen=True)
class _State:
    tenant: object = None
    bypassed: bool = False


_UNSET = _State()
_BYPASSED = _State(bypassed=True)
_state: ContextVar[_State] = ContextVar("tenant_domains_state", default=_UNSET)


def _switch(state: _State) -> _State:
    """Make `state` current and return the previous one. Every change goes through here."""
    previous = _state.get()
    _state.set(state)
    return previous


def get_current_tenant():
    """Return the current tenant, or None if there is none."""
    return _state.get().tenant


def is_tenant_bypassed() -> bool:
    """Whether code is running inside no_tenant()."""
    return _state.get().bypassed


def _later_in(state: _State, func):
    def run():
        with _use(state):
            return func()

    return run


def _database_scope_for(state: _State):
    value = "" if state.tenant is None else str(state.tenant.pk)
    return database_scope(value, wrap_callback=lambda func: _later_in(state, func))


@contextmanager
def _use(state: _State):
    # With row-level security on, the database setting moves with the Python state.
    with _database_scope_for(state):
        previous = _switch(state)
        try:
            yield
        finally:
            _switch(previous)


def tenant_context(tenant):
    """Run a block as `tenant`, for example in a management command or a task."""
    if tenant is None:
        raise ValueError("tenant_context needs a tenant; use no_tenant() for cross-tenant work.")
    return _use(_State(tenant=tenant))


def no_tenant():
    """Run a block explicitly outside any tenant."""
    return _use(_BYPASSED)
