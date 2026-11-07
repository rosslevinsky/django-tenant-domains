import asyncio
import os
import stat
from contextvars import copy_context
from urllib.parse import urlsplit

from asgiref.sync import async_to_sync, iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.shortcuts import resolve_url
from django.urls import Resolver404, resolve
from django.utils.module_loading import import_string

from .access import authorize
from .async_views import is_tenant_async_view
from .context import _database_scope_for, _State, _state, _switch, _use
from .exceptions import TenantAsyncViewError
from .models import get_tenant_model, normalize_slug
from .permissions import shield_restricted_superuser
from .resolvers import SESSION_KEY
from .rls import protected_aliases

EXEMPT_PATHS_SETTING = "TENANT_DOMAINS_EXEMPT_PATHS"
DEFAULT_RESOLVERS = [
    "django_tenant_domains.resolvers.URLKwargResolver",
    "django_tenant_domains.resolvers.SessionResolver",
]


def _resolved(request):
    try:
        return resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
    except Resolver404:
        return None


def _resolved_view(request):
    match = _resolved(request)
    return match.func if match else None


def _is_exempt(request) -> bool:
    """Pages that must work before anyone can enter a tenant, or that are outside all of them."""
    # A tenant from the host applies to every path, so the login page would redirect to itself.
    # LOGIN_URL includes any script prefix, as request.path does, and may carry a query string.
    if request.path == urlsplit(str(resolve_url(settings.LOGIN_URL))).path:
        return True
    path = request.path_info
    if any(path.startswith(prefix) for prefix in getattr(settings, EXEMPT_PATHS_SETTING, [])):
        return True
    match = _resolved(request)
    if match is None:
        return False
    # The staff admin is outside any tenant, so a remembered tenant must not decide it.
    return "admin" in match.app_names or getattr(match.func, "_tenant_exempt", False)


def _is_file_on_disk(filelike) -> bool:
    try:
        return stat.S_ISREG(os.fstat(filelike.fileno()).st_mode)
    except (AttributeError, OSError, ValueError):
        return False


def _scope_stream(response, state):
    """Run a streaming body as the tenant, since it executes after the middleware returns.

    The body gets its own context. Changes it makes, such as entering tenant_context, then
    last between chunks and never reach the code consuming the stream.
    """
    if not response.streaming:
        return response
    # A file on disk runs no tenant code, and replacing it would lose FileResponse's file handling.
    if _is_file_on_disk(getattr(response, "file_to_stream", None)):
        return response
    if getattr(response, "file_to_stream", None) is not None:
        # A WSGI server's file wrapper would read it directly, bypassing the wrapping below.
        response.file_to_stream = None
    context = copy_context()
    context.run(_switch, state)
    content = response.streaming_content
    wrap = _scoped_async if response.is_async else _scoped_sync
    response.streaming_content = wrap(content, context)
    close = response.close
    response.close = lambda: context.run(close)  # Closing runs the body's cleanup code.
    return response


def _advance(iterator):
    # Each chunk gets its own transaction, since the request's has closed by now.
    # Only the database scope: a generator may have set a tenant that must survive between chunks.
    with _database_scope_for(_state.get()):
        try:
            return True, next(iterator)
        except StopIteration:
            return False, None


def _scoped_sync(content, context):
    iterator = iter(content)
    while True:
        more, chunk = context.run(_advance, iterator)
        if not more:
            return
        yield chunk


async def _scoped_async(content, context):
    iterator = aiter(content)

    async def advance():
        return await anext(iterator)

    while True:
        try:
            chunk = await asyncio.create_task(advance(), context=context)
        except StopAsyncIteration:
            return
        yield chunk


class TenantMiddleware:
    """Finds the request's tenant and makes it current for the rest of the request.

    Place after SessionMiddleware and AuthenticationMiddleware. It checks access on every
    request, including to a tenant remembered in the session.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.resolvers = [
            import_string(path)()
            for path in getattr(settings, "TENANT_DOMAINS_RESOLVERS", DEFAULT_RESOLVERS)
        ]
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def process_exception(self, request, exception):
        # The exception becomes a response before it reaches us, so note it here.
        request._tenant_rollback = True

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        tenant, redirect = self._prepare(request)
        if redirect is not None:
            return redirect
        state = _State(tenant=tenant)
        if self._needs_request_transaction(request, tenant):
            return self._run_in_transaction(request, state, self.get_response)
        previous = _switch(state)
        try:
            return _scope_stream(self.get_response(request), state)
        finally:
            _switch(previous)

    async def __acall__(self, request):
        tenant, redirect = await sync_to_async(self._prepare)(request)
        if redirect is not None:
            return redirect
        state = _State(tenant=tenant)
        if self._needs_request_transaction(request, tenant):
            # A sync view on an async server: the request transaction has to run as sync code.
            run = sync_to_async(self._run_in_transaction)
            return await run(request, state, async_to_sync(self.get_response))
        previous = _switch(state)
        try:
            return _scope_stream(await self.get_response(request), state)
        finally:
            _switch(previous)

    @staticmethod
    def _needs_request_transaction(request, tenant) -> bool:
        if tenant is None or not protected_aliases():
            return False
        view = _resolved_view(request)
        if view is None or not iscoroutinefunction(view):
            return True
        if not is_tenant_async_view(view):
            raise TenantAsyncViewError(
                "Row-level security cannot use an async view's request transaction. Mark the "
                "view with @tenant_async_view and run its queries in in_tenant_transaction()."
            )
        return False

    @staticmethod
    def _run_in_transaction(request, state, get_response):
        """SET LOCAL lasts for one transaction, so one has to span the view and its rendering."""
        with _use(state):
            response = get_response(request)
            if response.status_code >= 500 or getattr(request, "_tenant_rollback", False):
                for alias in protected_aliases():
                    transaction.set_rollback(True, using=alias)
            return _scope_stream(response, state)

    def _prepare(self, request):
        """Return (tenant, response). Uses the database and session, so it runs as sync code."""
        request.tenant = None
        request.membership = None
        if _is_exempt(request):
            return None, None
        for resolver in self.resolvers:
            raw = resolver.resolve(request)
            if not raw:
                continue
            found = get_tenant_model().objects.filter(slug=normalize_slug(raw)).first()
            if found is None:
                if resolver.explicit:
                    raise Http404("No such tenant.")
                resolver.forget(request)
                continue
            outcome = self._authorize(request, found)
            if not outcome.allowed:
                if not resolver.explicit:
                    # A remembered tenant they have lost access to is simply no tenant.
                    resolver.forget(request)
                    continue
                if outcome.response is None:
                    raise Http404("No such tenant.")
                return None, outcome.response
            redirect = resolver.redirect(request, raw, found.slug)
            if redirect is not None:
                return None, redirect
            request.tenant = found
            request.membership = outcome.membership
            user = getattr(request, "user", None)
            if found.restrict_superusers and user is not None and user.is_superuser:
                shield_restricted_superuser(user)
            self._remember(request, found)
            return found, None
        return None, None

    @staticmethod
    def _authorize(request, tenant):
        return authorize(request, tenant, _resolved_view(request))

    @staticmethod
    def _remember(request, tenant) -> None:
        session = getattr(request, "session", None)
        if session is not None and session.get(SESSION_KEY) != tenant.slug:
            session[SESSION_KEY] = tenant.slug
