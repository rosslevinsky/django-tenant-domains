"""Keeps Postgres row-level security told which tenant is current.

Imports no models, because the context module needs it while models are loading.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.db import connections, transaction

SETTING_NAME = "app.current_tenant"
DEFAULT_BYPASS_ALIAS = "migrator"

_admin_bypass: ContextVar[bool] = ContextVar("tenant_domains_admin_bypass", default=False)


def is_enabled() -> bool:
    return getattr(settings, "TENANT_DOMAINS_RLS", True)


def bypass_alias() -> str:
    """The database alias whose login bypasses row-level security."""
    return getattr(settings, "TENANT_DOMAINS_RLS_BYPASS_DATABASE", DEFAULT_BYPASS_ALIAS)


def protected_aliases() -> list[str]:
    """The Postgres aliases that row-level security binds: all but the bypass login."""
    if not is_enabled():
        return []
    bypass = bypass_alias()
    return [
        alias
        for alias in connections
        if alias != bypass and connections[alias].vendor == "postgresql"
    ]


def bypass_alias_if_active() -> str | None:
    return bypass_alias() if protected_aliases() else None


@contextmanager
def admin_bypass():
    """Route tenant models to the bypass login for this block. See TenantAdminRouter."""
    token = _admin_bypass.set(True)
    try:
        yield
    finally:
        _admin_bypass.reset(token)


def is_admin_bypassed() -> bool:
    return _admin_bypass.get()


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _write_setting(connection, value: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [SETTING_NAME, value])


def _read_setting(connection) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [SETTING_NAME])
        return cursor.fetchone()[0] or ""


def _wrap_callbacks(connection, first: int, wrap_callback) -> None:
    """Keep this scope's on_commit callbacks in its tenant, since they run after the setting ends.

    Django has no public hook for this, so it rewrites run_on_commit. The test matrix covers each
    supported Django, whose entries are (savepoint ids, function, robust).
    """
    callbacks = connection.run_on_commit
    for index in range(first, len(callbacks)):
        savepoints, func, robust = callbacks[index]
        if not getattr(func, "_tenant_scoped", False):
            scoped = wrap_callback(func)
            scoped._tenant_scoped = True
            callbacks[index] = (savepoints, scoped, robust)


@contextmanager
def _scope_on(alias: str, value: str, wrap_callback=None):
    connection = connections[alias]
    # A transaction opened with set_autocommit(False) outlives us just as an outer atomic does.
    nested = connection.in_atomic_block or not connection.get_autocommit()
    with transaction.atomic(using=alias):
        previous = _read_setting(connection) if nested else ""
        _write_setting(connection, value)
        first_callback = len(connection.run_on_commit)
        yield
        if wrap_callback is not None:
            _wrap_callbacks(connection, first_callback, wrap_callback)
        # A rollback undoes the setting by itself, and no query can run until it happens.
        if nested and not connection.needs_rollback:
            _write_setting(connection, previous)


@contextmanager
def _scope_on_all(aliases: list[str], value: str, wrap_callback):
    if not aliases:
        yield
        return
    with (
        _scope_on(aliases[0], value, wrap_callback),
        _scope_on_all(aliases[1:], value, wrap_callback),
    ):
        yield


@contextmanager
def database_scope(value: str, wrap_callback=None):
    """Hold a transaction with the tenant set, on every protected database.

    Async code cannot hold a transaction, so there this does nothing. Queries then have no
    tenant, which row-level security answers with no rows. See in_tenant_transaction().
    """
    aliases = protected_aliases()
    if not aliases or _in_event_loop():
        yield
        return
    with _scope_on_all(aliases, value, wrap_callback):
        yield
