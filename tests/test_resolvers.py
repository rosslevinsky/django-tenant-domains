import pytest
from django.test import RequestFactory, override_settings

from django_tenant_domains.resolvers import (
    HeaderResolver,
    SessionResolver,
    SubdomainResolver,
    URLKwargResolver,
)

rf = RequestFactory()


def test_url_resolver_reads_the_kwarg():
    assert URLKwargResolver().resolve(rf.get("/t/acme/")) == "acme"


def test_url_resolver_ignores_other_urls():
    assert URLKwargResolver().resolve(rf.get("/plain/")) is None
    assert URLKwargResolver().resolve(rf.get("/no/such/url/")) is None


def test_session_resolver_reads_the_session():
    request = rf.get("/plain/")
    request.session = {"tenant_domains_last_tenant": "acme"}
    assert SessionResolver().resolve(request) == "acme"


def test_session_resolver_without_session_middleware():
    assert SessionResolver().resolve(rf.get("/plain/")) is None


def test_session_resolver_forgets_a_tenant():
    request = rf.get("/plain/")
    request.session = {"tenant_domains_last_tenant": "gone"}
    SessionResolver().forget(request)
    assert request.session == {}


@override_settings(TENANT_DOMAINS_BASE_DOMAIN="example.com", ALLOWED_HOSTS=["*"])
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("acme.example.com", "acme"),
        ("acme.example.com:8000", "acme"),
        ("example.com", None),
        ("a.b.example.com", None),
        ("acme.EXAMPLE.com", "acme"),
        ("ACME.example.com.", "ACME"),
        ("acme.evil.com", None),
        ("evilexample.com", None),
    ],
)
def test_subdomain_resolver(host, expected):
    assert SubdomainResolver().resolve(rf.get("/", headers={"host": host})) == expected


@override_settings(TENANT_DOMAINS_BASE_DOMAIN="Example.COM.", ALLOWED_HOSTS=["*"])
def test_subdomain_resolver_normalizes_the_base_domain():
    assert SubdomainResolver().resolve(rf.get("/", headers={"host": "a.example.com"})) == "a"


def test_subdomain_resolver_needs_a_base_domain():
    assert SubdomainResolver().resolve(rf.get("/")) is None


def test_header_resolver():
    assert HeaderResolver().resolve(rf.get("/", headers={"x-tenant": "acme"})) == "acme"
    assert HeaderResolver().resolve(rf.get("/")) is None


@override_settings(TENANT_DOMAINS_HEADER="X-Org")
def test_header_resolver_name_is_configurable():
    assert HeaderResolver().resolve(rf.get("/", headers={"x-org": "acme"})) == "acme"


def test_only_session_resolver_is_not_explicit():
    assert SessionResolver.explicit is False
    assert URLKwargResolver.explicit is True
    assert SubdomainResolver.explicit is True
    assert HeaderResolver.explicit is True
