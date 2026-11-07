"""Attaches third-party models to tenants through generated companion tables.

The third-party table gets row-level security policies, but no new column and no trigger. Imports
no package models, because projects call register_external while their models are loading.
"""

import hashlib
import sys

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, models, router, transaction
from django.db.models import Q

from ..context import get_current_tenant, is_tenant_bypassed
from ..exceptions import TenantMismatch, TenantNotSet
from ..managers import check_foreign_key
from ..rls import protected_aliases
from ..sharing import READ, TENANT_MODEL_SETTING, WRITE

OWNER = "owner"
ACCESS_CHOICES = [(OWNER, "Owner"), (WRITE, "Read-write"), (READ, "Read-only")]

_links: dict = {}


def link_of(model):
    """The companion model of a registered third-party model or one of its proxies."""
    return _links.get(model._meta.concrete_model)


def external_models() -> list:
    return list(_links)


def _require_scope():
    current = get_current_tenant()
    if current is None and not is_tenant_bypassed():
        raise TenantNotSet("No current tenant. Use tenant_context(tenant) or no_tenant().")
    return current


class LinkQuerySet(models.QuerySet):
    def _refuse(self):
        if not is_tenant_bypassed():
            raise TenantMismatch("Use share() and unshare() to change a tenant link.")

    def update(self, **kwargs):
        self._refuse()
        return super().update(**kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        self._refuse()
        return super().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, *args, **kwargs):
        self._refuse()
        return super().bulk_update(objs, fields, *args, **kwargs)

    def delete(self):
        self._refuse()
        return super().delete()


class LinkManager(models.Manager.from_queryset(LinkQuerySet)):
    """Shows a tenant its own links, and the links on records it owns."""

    def get_queryset(self):
        queryset = super().get_queryset()
        current = _require_scope()
        if current is None:
            return queryset
        owned = self.model._base_manager.filter(tenant_id=current.pk, access=OWNER)
        return queryset.filter(Q(tenant_id=current.pk) | Q(record_id__in=owned.values("record_id")))


class AbstractTenantLink(models.Model):
    _tenant_is_link_table = True

    access = models.CharField(max_length=5, choices=ACCESS_CHOICES, default=OWNER)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = LinkManager()
    unscoped_objects = models.Manager()

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return f"{self.record_id} {self.access} for {self.tenant_id}"

    def save(self, *args, **kwargs):
        if not is_tenant_bypassed():
            raise TenantMismatch("Use share() to change a tenant link.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not is_tenant_bypassed():
            raise TenantMismatch("Use unshare() to change a tenant link.")
        return super().delete(*args, **kwargs)


def _known_key(instance):
    """None until the key has a value. A database default is only an expression at this point."""
    key = instance.pk
    return None if hasattr(key, "resolve_expression") else key


def _reserve_key(model, using) -> int:
    pk = model._meta.pk
    if not isinstance(pk, models.AutoField):
        raise TenantMismatch(
            f"Creating {model.__name__} under row-level security needs an auto-generated primary "
            "key, or a value set before saving. A database default cannot be read in time."
        )
    with connections[using].cursor() as cursor:
        cursor.execute(
            "SELECT nextval(pg_get_serial_sequence(%s, %s))",
            [connections[using].ops.quote_name(model._meta.db_table), pk.column],
        )
        return cursor.fetchone()[0]


def _add_owner(link, key, tenant_pk, using) -> None:
    link._base_manager.using(using).bulk_create(
        [link(record_id=key, tenant_id=tenant_pk, access=OWNER)]
    )


def _tenant_keys(model):
    """Foreign keys to tenant-scoped records or to registered third-party records."""
    for field in model._meta.concrete_fields:
        if not field.is_relation or field.remote_field.parent_link:
            continue
        if getattr(field.related_model, "_is_tenant_scoped", False) or link_of(field.related_model):
            yield field


def _check_foreign_keys(sender, instance, owner_pk, using, update_fields) -> None:
    """The database accepts a key to a record that row-level security hides, and that record's
    owner could then never delete it. So each new key must point where the record's owner can
    reach, as a grantee's key into its own records would put them in the owner's record."""
    fields = [
        field
        for field in _tenant_keys(sender)
        if update_fields is None or field.name in update_fields or field.attname in update_fields
    ]
    if not fields:
        return
    # An unchanged key may point at the owner's private record, which a grantee cannot see.
    stored = {}
    key = _known_key(instance)
    if key is not None and not instance._state.adding:
        rows = sender._base_manager.using(using).filter(pk=key)
        stored = rows.values(*(field.attname for field in fields)).first() or {}
    for field in fields:
        value = getattr(instance, field.attname)
        if value is None or (field.attname in stored and stored[field.attname] == value):
            continue
        if owner_pk is None:
            # Row-level security hides the owner's link from a grantee, so the owner is unknown.
            raise TenantMismatch(f"Only the owner of {instance!r} can change {field.name}.")
        if getattr(field.related_model, "_is_tenant_scoped", False):
            check_foreign_key(field, value, owner_pk, using)
            continue
        targets = field.related_model._base_manager.using(using)
        targets = targets.filter(**{field.target_field.attname: value}).values("pk")
        links = link_of(field.related_model)._base_manager.using(using)
        reachable = links.filter(tenant_id=owner_pk, record_id__in=targets)
        if not reachable.exists():
            raise TenantMismatch(f"{field.name} points to another tenant's record.")


def _before_save(sender, instance, raw, using, **kwargs):
    """Under row-level security the link has to exist first, for the insert to pass its policy."""
    if raw:
        return
    current = _require_scope()
    if current is None:
        return
    link = link_of(sender)
    key = _known_key(instance)
    access = None
    if key is not None:
        rows = link._base_manager.using(using)
        access = rows.filter(record_id=key, tenant_id=current.pk).values_list("access", flat=True)
        access = access.first()
        if access == READ:
            raise TenantMismatch(f"{instance!r} is shared read-only with this tenant.")
        if access not in (OWNER, WRITE) and (
            not instance._state.adding or sender._base_manager.using(using).filter(pk=key).exists()
        ):
            raise TenantMismatch(f"{instance!r} belongs to another tenant.")
    owner_pk = current.pk
    if access == WRITE:
        owners = link._base_manager.using(using).filter(record_id=key, access=OWNER)
        owner_pk = owners.values_list("tenant_id", flat=True).first()
    _check_foreign_keys(sender, instance, owner_pk, using, kwargs.get("update_fields"))
    if access in (OWNER, WRITE):
        return
    if using not in protected_aliases():
        return  # Saved after the insert, which may be how the key gets its value.
    if not connections[using].in_atomic_block:
        raise TenantMismatch(
            f"Creating {sender.__name__} under row-level security needs a transaction. "
            "Use tenant_context(tenant)."
        )
    if key is None:
        instance.pk = _reserve_key(sender, using)
    _add_owner(link, instance.pk, current.pk, using)


def _after_save(sender, instance, created, raw, using, **kwargs):
    current = get_current_tenant()
    if raw or not created or current is None:
        return
    link = link_of(sender)
    if not link._base_manager.using(using).filter(record_id=instance.pk).exists():
        _add_owner(link, instance.pk, current.pk, using)


def _before_delete(sender, instance, using, **kwargs):
    current = _require_scope()
    if current is None:
        return
    rows = link_of(sender)._base_manager.using(using)
    if not rows.filter(record_id=instance.pk, tenant_id=current.pk, access=OWNER).exists():
        raise TenantMismatch(f"Only the tenant that owns {instance!r} can delete it.")


def _after_delete(sender, instance, using, **kwargs):
    # After, because the delete's policy looks for the owner's link.
    link_of(sender)._base_manager.using(using).filter(record_id=instance.pk).delete()


def _connect(model) -> None:
    label = model._meta.label
    for signal, receiver in (
        (models.signals.pre_save, _before_save),
        (models.signals.post_save, _after_save),
        (models.signals.pre_delete, _before_delete),
        (models.signals.post_delete, _after_delete),
    ):
        signal.connect(receiver, sender=model, dispatch_uid=f"tenant_domains_external_{label}")


def connect_proxies() -> None:
    """Signals name the proxy as their sender, so each proxy needs its own connection."""
    for model in apps.get_models():
        if model._meta.proxy and link_of(model) is not None:
            _connect(model)


def _tenant_model() -> str:
    # This can run before the package's models module, which also sets this default.
    if not hasattr(settings, TENANT_MODEL_SETTING):
        setattr(settings, TENANT_MODEL_SETTING, "django_tenant_domains.Tenant")
    return getattr(settings, TENANT_MODEL_SETTING)


def _make_link_model(model, module, name):
    digest = hashlib.sha256(model._meta.label_lower.encode()).hexdigest()[:16]
    meta = type(
        "Meta",
        (),
        {
            "base_manager_name": "unscoped_objects",
            "constraints": [
                models.UniqueConstraint(fields=["record", "tenant"], name=f"link_uniq_{digest}"),
                models.UniqueConstraint(
                    fields=["record"], condition=Q(access=OWNER), name=f"link_owner_{digest}"
                ),
            ],
            "indexes": [models.Index(fields=["tenant", "record"], name=f"link_idx_{digest}")],
        },
    )
    return type(
        name,
        (AbstractTenantLink,),
        {
            "__module__": module,
            "Meta": meta,
            # The hook removes links after the delete, whose policy needs the owner's link.
            "record": models.ForeignKey(model, on_delete=models.DO_NOTHING, related_name="+"),
            "tenant": models.ForeignKey(
                _tenant_model(),
                on_delete=models.CASCADE,
                related_name="+",
            ),
        },
    )


def register_external(model, name=None):
    """Attach a third-party model to tenants. Call this at module level in your app's models.py.

    Generates <Model>TenantLink in the calling module, so run makemigrations for your app afterward.
    Pass name when two third-party models with one class name register in the same app.
    """
    concrete = model._meta.concrete_model
    if concrete in _links:
        return _links[concrete]
    module = sys._getframe(1).f_globals["__name__"]
    name = name or f"{concrete.__name__}TenantLink"
    app_config = apps.get_containing_app_config(module)
    if app_config is None:
        raise ImproperlyConfigured(f"Call register_external from an installed app, not {module}.")
    label = app_config.label
    # Django would replace the first model, and both tables would then share one set of links.
    if name.lower() in apps.all_models[label]:
        raise ImproperlyConfigured(
            f"{label} already has a model named {name}. Pass register_external(model, name=...)."
        )
    link = _make_link_model(concrete, module, name)
    setattr(sys.modules[module], link.__name__, link)
    _links[concrete] = link
    _connect(concrete)
    return link


class ExternalQuerySet(models.QuerySet):
    """update() skips the save hook, so it narrows itself to what the tenant can change."""

    def update(self, **kwargs):
        current = _require_scope()
        if current is None:
            return super().update(**kwargs)
        # The write database, as a subquery cannot cross databases.
        using = self._db or router.db_for_write(self.model, **self._hints)
        writable = _links_of(self.model, using, current).filter(access__in=(OWNER, WRITE))
        return models.QuerySet.update(self.filter(pk__in=writable.values("record_id")), **kwargs)


def _links_of(model, using, tenant):
    return link_of(model)._base_manager.using(using).filter(tenant_id=tenant.pk)


def external_records(model, using=None, write_only=False):
    """The records the current tenant can reach, which third-party managers do not filter."""
    current = _require_scope()
    rows = ExternalQuerySet(model=model, using=using)
    if current is None:
        return rows
    links = _links_of(model, using, current)
    if write_only:
        links = links.filter(access__in=(OWNER, WRITE))
    return rows.filter(pk__in=links.values("record_id"))


def _owned_links(record, using):
    current = _require_scope()
    links = link_of(type(record))._base_manager.using(using).filter(record_id=record.pk)
    owner_id = links.filter(access=OWNER).values_list("tenant_id", flat=True).first()
    if owner_id is None or (current is not None and owner_id != current.pk):
        raise TenantMismatch("Only the tenant that owns a record can change its shares.")
    return links, owner_id


def share(record, grantee, access=READ):
    """Grant a tenant access to a third-party record, or change the access it already has."""
    if access not in (READ, WRITE):
        raise ValueError(f"Unknown access level: {access!r}")
    link = link_of(type(record))
    using = router.db_for_write(link, instance=record)
    with transaction.atomic(using=using):
        links, owner_id = _owned_links(record, using)
        if grantee.pk == owner_id:
            raise TenantMismatch("A tenant cannot share a record with itself.")
        if not links.filter(tenant_id=grantee.pk).update(access=access):
            links.bulk_create([link(record_id=record.pk, tenant_id=grantee.pk, access=access)])


def unshare(record, grantee) -> None:
    """End a share of a third-party record."""
    using = router.db_for_write(link_of(type(record)), instance=record)
    with transaction.atomic(using=using):
        links, _ = _owned_links(record, using)
        links.filter(tenant_id=grantee.pk).exclude(access=OWNER).delete()


def assign_owner(record, tenant, using=None) -> None:
    """Give a record that has no owner, such as one from before registration, to a tenant.

    Under row-level security this needs the bypass login: pass using="migrator".
    """
    if get_current_tenant() is not None or not is_tenant_bypassed():
        raise TenantMismatch("Assign an owner under no_tenant().")
    link = link_of(type(record))
    using = using or router.db_for_write(link, instance=record)
    links = link._base_manager.using(using).filter(record_id=record.pk)
    with transaction.atomic(using=using):
        if links.filter(access=OWNER).exists():
            raise TenantMismatch(f"{record!r} already has an owner.")
        # A tenant the record was shared with can become its owner.
        if not links.filter(tenant_id=tenant.pk).update(access=OWNER):
            _add_owner(link, record.pk, tenant.pk, using)
