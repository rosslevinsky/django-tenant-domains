from django.test import override_settings

from django_tenant_domains.checks import check_inner_exception_middleware

TENANT = "django_tenant_domains.middleware.TenantMiddleware"


class HandlesExceptions:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        return None


HANDLER = f"{__name__}.HandlesExceptions"


def _ids():
    return [error.id for error in check_inner_exception_middleware(None)]


def test_warns_when_a_later_middleware_can_handle_exceptions():
    with override_settings(MIDDLEWARE=[TENANT, HANDLER]):
        assert _ids() == ["tenant_domains.W002"]


def test_quiet_when_the_handler_is_outside():
    with override_settings(MIDDLEWARE=[HANDLER, TENANT]):
        assert _ids() == []


def test_quiet_without_the_tenant_middleware():
    with override_settings(MIDDLEWARE=[HANDLER]):
        assert _ids() == []


def factory_with_hook(get_response):
    return HandlesExceptions(get_response)


def factory_without_hook(get_response):
    def middleware(request):
        return get_response(request)

    return middleware


def factory_that_fails(get_response):
    raise RuntimeError("needs settings this check does not have")


def test_warns_when_a_later_factory_builds_an_exception_hook():
    with override_settings(MIDDLEWARE=[TENANT, f"{__name__}.factory_with_hook"]):
        assert _ids() == ["tenant_domains.W002"]


def test_quiet_when_a_later_factory_builds_no_exception_hook():
    with override_settings(MIDDLEWARE=[TENANT, f"{__name__}.factory_without_hook"]):
        assert _ids() == []


def test_warns_when_a_later_factory_cannot_be_classified():
    with override_settings(MIDDLEWARE=[TENANT, f"{__name__}.factory_that_fails"]):
        assert _ids() == ["tenant_domains.W005"]
