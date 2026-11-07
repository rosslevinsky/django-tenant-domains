"""Ways of finding which tenant a request is for. Each returns a raw slug or None."""

from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponsePermanentRedirect
from django.urls import Resolver404, resolve
from django.utils.encoding import iri_to_uri
from django.utils.http import escape_leading_slashes

URL_KWARG = "tenant_slug"
SESSION_KEY = "tenant_domains_last_tenant"
# Characters allowed unescaped in a URL path (RFC 3986).
PATH_SAFE = "/:@!$&'()*+,;=~"


class Resolver:
    # An explicit request for a tenant that does not exist is a 404. A remembered one is not.
    explicit = True

    def resolve(self, request) -> str | None:
        raise NotImplementedError

    def forget(self, request) -> None:
        """Called when the slug this resolver returned names no tenant."""

    def redirect(self, request, raw: str, slug: str):
        """Return a redirect to the canonical URL when `raw` is not canonical, or None."""
        return


class URLKwargResolver(Resolver):
    """Reads the tenant_slug URL kwarg, as in path("t/<str:tenant_slug>/", view)."""

    def resolve(self, request) -> str | None:
        match = self._match(request)
        return match.kwargs.get(URL_KWARG) if match else None

    def redirect(self, request, raw: str, slug: str):
        if raw == slug:
            return None
        url = self._canonical_path(request, raw, slug)
        if url is None:
            return None
        if query := request.META.get("QUERY_STRING"):
            url = f"{url}?{iri_to_uri(query)}"
        response = HttpResponsePermanentRedirect(url)
        if request.method not in ("GET", "HEAD"):
            response.status_code = 308  # 301 lets clients turn a POST into a GET.
        return response

    def _canonical_path(self, request, raw: str, slug: str) -> str | None:
        """Rebuild this URL with the canonical slug. None if no rebuilt path checks out."""
        urlconf = getattr(request, "urlconf", None)
        match = self._match(request)
        wanted = {**match.kwargs, URL_KWARG: slug}
        path = request.path_info
        # The slug's text can also appear elsewhere in the path. The right place is the one
        # that resolves to the same route with every other argument unchanged.
        start = path.find(raw)
        while start != -1:
            candidate = path[:start] + slug + path[start + len(raw) :]
            try:
                rebuilt = resolve(candidate, urlconf=urlconf)
            except Resolver404:
                rebuilt = None
            if (
                rebuilt is not None
                and rebuilt.route == match.route
                and rebuilt.kwargs == wanted
                and rebuilt.args == match.args
            ):
                prefix = request.path[: len(request.path) - len(path)]
                return escape_leading_slashes(quote(prefix + candidate, safe=PATH_SAFE))
            start = path.find(raw, start + 1)
        return None

    @staticmethod
    def _match(request):
        try:
            return resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
        except Resolver404:
            return None


class SessionResolver(Resolver):
    """Falls back to the tenant this session last visited."""

    explicit = False

    def resolve(self, request) -> str | None:
        session = getattr(request, "session", None)
        return session.get(SESSION_KEY) if session is not None else None

    def forget(self, request) -> None:
        del request.session[SESSION_KEY]


class SubdomainResolver(Resolver):
    """Reads the tenant from <slug>.<TENANT_DOMAINS_BASE_DOMAIN>."""

    def resolve(self, request) -> str | None:
        base = getattr(settings, "TENANT_DOMAINS_BASE_DOMAIN", None)
        if not base:
            return None
        host = request.get_host().partition(":")[0].rstrip(".")
        label, dot, rest = host.partition(".")
        return label if dot and rest.lower() == base.rstrip(".").lower() else None


class HeaderResolver(Resolver):
    """Reads the tenant from a request header (TENANT_DOMAINS_HEADER), for APIs."""

    def resolve(self, request) -> str | None:
        return request.headers.get(getattr(settings, "TENANT_DOMAINS_HEADER", "X-Tenant"))
