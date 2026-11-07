from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.core.exceptions import FieldDoesNotExist, MiddlewareNotUsed
from django.db import connections
from django.utils.module_loading import import_string

from .external import external_models
from .rls import bypass_alias, is_enabled, protected_aliases
from .rls.policies import missing_policies
from .sharing import config_of, has_sharing, is_reverse_foreign_key, shareable_models

MIDDLEWARE_PATH = "django_tenant_domains.middleware.TenantMiddleware"
BACKEND_PATH = "django_tenant_domains.backends.TenantBackend"
ROUTER_PATH = "django_tenant_domains.routers.TenantAdminRouter"


def _reaches(start, target, children) -> bool:
    seen = set()
    waiting = list(children.get(start, ()))
    while waiting:
        model = waiting.pop()
        if model is target:
            return True
        if model not in seen:
            seen.add(model)
            waiting += children.get(model, ())
    return False


@register()
def check_shareable_models(app_configs, **kwargs):
    errors = []
    children = {}
    for model in shareable_models():
        config = config_of(model)
        # A proxy of a shared model inherits its sharing, which is fine. Its own does nothing.
        if model._meta.proxy and "_tenant_sharing" in vars(model):
            errors.append(
                Error(
                    f"{model.__name__} is a proxy model, and @shareable on a proxy shares "
                    "nothing under row-level security.",
                    hint=f"Put @shareable on {model._meta.concrete_model.__name__} instead.",
                    id="tenant_domains.E014",
                )
            )
            continue
        if config.allow_links and not protected_aliases():
            message = (
                f"{model.__name__} allows links, but no database has row-level security. "
                "Following a link can read a record after its share ends."
            )
            if not is_enabled() and any(
                connections[alias].vendor == "postgresql" for alias in connections
            ):
                errors.append(
                    Error(
                        message,
                        hint="Turn on TENANT_DOMAINS_RLS, or remove allow_links.",
                        id="tenant_domains.E010",
                    )
                )
            else:
                errors.append(Warning(message, id="tenant_domains.W004"))
        for name in config.cascade:
            try:
                relation = model._meta.get_field(name)
            except FieldDoesNotExist:
                relation = None
            child = getattr(relation, "related_model", None)
            if (
                relation is None
                or not is_reverse_foreign_key(relation)
                or not hasattr(child, "_is_tenant_scoped")
            ):
                errors.append(
                    Error(
                        f"{model.__name__} cascades to {name!r}, which is not a reverse foreign "
                        "key from a TenantModel.",
                        id="tenant_domains.E005",
                    )
                )
                continue
            children.setdefault(model, []).append(child)
            if not relation.field.target_field.primary_key:
                errors.append(
                    Error(
                        f"{model.__name__} cascades to {name!r}, whose foreign key does not "
                        "target the primary key, so the share would not reach it.",
                        id="tenant_domains.E007",
                    )
                )
    for model, reached in children.items():
        for child in reached:
            if child in children and child is not model:
                errors.append(
                    Error(
                        f"{model.__name__} cascades to {child.__name__}, which also cascades. "
                        "A share only reaches one level, so what is below would stay hidden.",
                        id="tenant_domains.E009",
                    )
                )
    for model in children:
        if _reaches(model, model, children):
            errors.append(
                Error(
                    f"{model.__name__} cascades back to itself. Row-level security policies "
                    "cannot depend on their own table.",
                    id="tenant_domains.E008",
                )
            )
    for model in apps.get_models():
        scoped = getattr(model, "_is_tenant_scoped", False)
        if scoped and not model._meta.proxy and has_sharing(model):
            inherits = model._meta.get_parent_list()
            subclasses = [
                m
                for m in apps.get_models()
                if not m._meta.proxy and model in m._meta.get_parent_list()
            ]
            if inherits or subclasses:
                errors.append(
                    Error(
                        f"{model.__name__} is shared and uses multi-table inheritance, which "
                        "sharing does not support.",
                        id="tenant_domains.E006",
                    )
                )
    return errors


@register()
def check_external_models(app_configs, **kwargs):
    errors = []
    for model in external_models():
        if model._meta.get_parent_list() or any(
            not m._meta.proxy and model in m._meta.get_parent_list() for m in apps.get_models()
        ):
            errors.append(
                Error(
                    f"{model.__name__} is registered as external and uses multi-table "
                    "inheritance, which companion tables do not support.",
                    id="tenant_domains.E011",
                )
            )
        if getattr(model, "_is_tenant_scoped", False) or has_sharing(model):
            errors.append(
                Error(
                    f"{model.__name__} is registered as external, but already has a tenant column "
                    "or share table.",
                    id="tenant_domains.E012",
                )
            )
    return errors


@register()
def check_bypass_login(app_configs, **kwargs):
    postgres = [alias for alias in connections if connections[alias].vendor == "postgresql"]
    if is_enabled() and postgres and not protected_aliases():
        return [
            Error(
                f"TENANT_DOMAINS_RLS is on, but the only Postgres database alias is the bypass "
                f"login {bypass_alias()!r}, so row-level security protects nothing.",
                hint="Connect the application through its own alias with an ordinary login, and "
                "keep the bypass login under a separate alias such as 'migrator'.",
                id="tenant_domains.E013",
            )
        ]
    if not protected_aliases() or bypass_alias() in connections:
        return []
    return [
        Error(
            f"Row-level security needs a database alias named {bypass_alias()!r}.",
            hint="Add an alias with a separate login that has BYPASSRLS, for migrations. "
            "TENANT_DOMAINS_RLS_BYPASS_DATABASE can name it; never point it at the alias "
            "the application uses.",
            id="tenant_domains.E001",
        )
    ]


@register()
def check_admin_router(app_configs, **kwargs):
    if not protected_aliases() or ROUTER_PATH in getattr(settings, "DATABASE_ROUTERS", []):
        return []
    return [
        Warning(
            "TenantModelAdmin cannot validate foreign keys under row-level security without "
            "its router.",
            hint=f"Add {ROUTER_PATH!r} to DATABASE_ROUTERS.",
            id="tenant_domains.W001",
        )
    ]


@register()
def check_tenant_backend(app_configs, **kwargs):
    backends = list(getattr(settings, "AUTHENTICATION_BACKENDS", []))
    if BACKEND_PATH in backends and backends[0] == BACKEND_PATH:
        return []
    return [
        Warning(
            "user.has_perm() will not follow tenant roles: a global permission would count "
            "inside every tenant.",
            hint=f"Put {BACKEND_PATH!r} first in AUTHENTICATION_BACKENDS, replacing ModelBackend.",
            id="tenant_domains.W003",
        )
    ]


@register()
def check_authentication_middleware(app_configs, **kwargs):
    middleware = list(getattr(settings, "MIDDLEWARE", []))
    if MIDDLEWARE_PATH not in middleware:
        return []
    auth = "django.contrib.auth.middleware.AuthenticationMiddleware"
    if auth in middleware and middleware.index(auth) < middleware.index(MIDDLEWARE_PATH):
        return []
    return [
        Error(
            "TenantMiddleware needs request.user, or every signed-in user is refused.",
            hint=f"Put {auth!r} before it in MIDDLEWARE.",
            id="tenant_domains.E004",
        )
    ]


@register()
def check_inner_exception_middleware(app_configs, **kwargs):
    """Django stops calling exception hooks at the first one that returns a response."""
    middleware = list(getattr(settings, "MIDDLEWARE", []))
    if MIDDLEWARE_PATH not in middleware:
        return []
    inner = middleware[middleware.index(MIDDLEWARE_PATH) + 1 :]
    handlers = []
    unknown = []
    for path in inner:
        found = _has_exception_hook(import_string(path))
        if found:
            handlers.append(path)
        elif found is None:
            unknown.append(path)
    warnings = []
    if handlers:
        warnings.append(
            Warning(
                f"{handlers} can handle a view's exception before TenantMiddleware sees it, and "
                "then the request's transaction commits instead of rolling back.",
                hint="Put TenantMiddleware after these in MIDDLEWARE.",
                id="tenant_domains.W002",
            )
        )
    if unknown:
        warnings.append(
            Warning(
                f"Cannot tell whether {unknown} handle a view's exception. If one does, the "
                "request's transaction commits instead of rolling back.",
                hint="Put TenantMiddleware after these in MIDDLEWARE.",
                id="tenant_domains.W005",
            )
        )
    return warnings


def _has_exception_hook(entry) -> bool | None:
    """Django registers the hook of what a middleware factory returns, not of the factory.

    None when a factory cannot be built here to find out.
    """
    if isinstance(entry, type):
        return hasattr(entry, "process_exception")
    try:
        built = entry(lambda request: None)
    except MiddlewareNotUsed:
        return False
    except Exception:  # noqa: BLE001 - any failure means the check cannot tell.
        return None
    return hasattr(built, "process_exception")


@register(Tags.database)
def check_protected_logins_and_tables(app_configs, databases=None, **kwargs):
    # migrate --database=<bypass> is how a missing policy gets repaired, so it must not fail here.
    aliases = [alias for alias in protected_aliases() if alias in (databases or ())]
    if not aliases or bypass_alias() not in connections:
        return []
    errors = []
    for alias in aliases:
        with connections[alias].cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
            if cursor.fetchone()[0]:
                errors.append(
                    Error(
                        f"Database alias {alias!r} logs in as a role that row-level security "
                        "does not apply to, so it would protect nothing.",
                        id="tenant_domains.E002",
                    )
                )
    with connections[bypass_alias()].cursor() as cursor:
        cursor.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            errors.append(
                Error(
                    f"The bypass database alias {bypass_alias()!r} logs in as a role that "
                    "row-level security binds, so migrations and cross-tenant work see no rows.",
                    hint="Give that login BYPASSRLS. Owning the tables is not enough, as they force row-level security.",
                    id="tenant_domains.E015",
                )
            )
    # Each database the application reaches, once, as aliases can share one.
    seen = set()
    for alias in aliases:
        config = connections[alias].settings_dict
        database = (config.get("NAME"), config.get("HOST"), config.get("PORT"))
        if database in seen:
            continue
        seen.add(database)
        missing = missing_policies(alias)
        if missing:
            errors.append(
                Error(
                    f"These tenant-scoped tables have missing or out-of-date row-level "
                    f"security in the database of alias {alias!r}: {missing}.",
                    hint="Run migrate on the bypass database, or the tenant_rls command.",
                    id="tenant_domains.E003",
                )
            )
    return errors
