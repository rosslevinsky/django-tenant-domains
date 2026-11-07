"""Lets an owning tenant grant other tenants access to chosen records. Opt-in per model.

Imports no package models, because managers.py needs it while models are loading.
"""

import hashlib
import sys
from dataclasses import dataclass

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, models, router, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.signals import class_prepared
from django.dispatch import receiver

from ..context import get_current_tenant, is_tenant_bypassed
from ..exceptions import TenantMismatch, TenantNotSet

READ = "read"
WRITE = "write"
ACCESS_CHOICES = [(READ, "Read-only"), (WRITE, "Read-write")]
TENANT_MODEL_SETTING = "TENANT_DOMAINS_TENANT_MODEL"


@dataclass(frozen=True)
class SharingConfig:
    share_model: type
    cascade: tuple
    allow_links: bool


def config_of(model) -> SharingConfig | None:
    return getattr(model, "_tenant_sharing", None)


def shareable_models() -> list:
    return [model for model in apps.get_models() if config_of(model) is not None]


def is_reverse_foreign_key(relation) -> bool:
    # GenericRelation is also one-to-many, but no foreign key stands behind it.
    return relation.one_to_many and relation.auto_created and not relation.concrete


def cascade_links(model) -> list:
    """The foreign keys through which this model inherits shares from a parent."""
    links = []
    for parent in shareable_models():
        for name in config_of(parent).cascade:
            try:
                relation = parent._meta.get_field(name)
            except FieldDoesNotExist:
                continue  # A system check reports this.
            if not is_reverse_foreign_key(relation):
                continue
            # Grants are stored by primary key, so any other target would match the wrong record.
            if (
                relation.related_model._meta.concrete_model is model._meta.concrete_model
                and relation.field.target_field.primary_key
            ):
                links.append(relation.field)
    return links


def has_sharing(model) -> bool:
    return config_of(model) is not None or bool(cascade_links(model))


def _grants(share_model, tenant_pk, write_only: bool):
    grants = share_model._base_manager.filter(grantee_id=tenant_pk)
    return grants.filter(access=WRITE) if write_only else grants


def _granted_q(model, tenant_pk, write_only: bool) -> Q:
    query = Q()
    config = config_of(model)
    if config is not None:
        query |= Q(pk__in=_grants(config.share_model, tenant_pk, write_only).values("record_id"))
    for link in cascade_links(model):
        share_model = config_of(link.related_model).share_model
        # Only the parent owner's children inherit. Another grantee may have linked its own record
        # to the same parent, and that record is not the owner's to share. This is a subquery
        # because a join would drop the row itself when row-level security hides the parent.
        parents = link.related_model._base_manager.filter(
            pk=OuterRef(link.attname), tenant_id=OuterRef("tenant_id")
        )
        granted = _grants(share_model, tenant_pk, write_only).values("record_id")
        query |= Q(Q(**{f"{link.attname}__in": granted}), Exists(parents))
    return query


def visible_q(model, tenant_pk) -> Q:
    return Q(tenant_id=tenant_pk) | _granted_q(model, tenant_pk, write_only=False)


def writable_q(model, tenant_pk) -> Q:
    return Q(tenant_id=tenant_pk) | _granted_q(model, tenant_pk, write_only=True)


def can_link(field, key, tenant_pk, using) -> bool:
    """Whether this tenant may point this foreign key at this record of another tenant."""
    config = config_of(field.related_model)
    if config is None or not config.allow_links or not field.target_field.primary_key:
        return False
    # Any read access counts, including a share inherited through a cascade.
    rows = field.related_model._base_manager.using(using).filter(pk=key)
    return rows.filter(_granted_q(field.related_model, tenant_pk, write_only=False)).exists()


def object_allows(perm: str, obj, tenant) -> bool:
    """Whether this tenant's grants let it act on obj. Permission to act at all is the role's."""
    from ..external import link_of

    model = type(obj)
    action = perm.rpartition(".")[2].partition("_")[0]
    link = link_of(model)
    if link is not None:
        return _external_allows(link, action, obj, tenant)
    if getattr(obj, "tenant_id", None) in (None, tenant.pk):
        return True
    if obj.pk is None or not has_sharing(model):
        return False
    allowed = {"view": visible_q, "change": writable_q}.get(action)
    if allowed is None:
        return False
    # The object's own database, as another database can hold a different row with this key.
    using = obj._state.db or router.db_for_read(model, instance=obj)
    rows = model._base_manager.using(using).filter(pk=obj.pk)
    return rows.filter(allowed(model, tenant.pk)).exists()


def _external_allows(link, action, obj, tenant) -> bool:
    """A third-party record has no tenant column, so its links say who may act on it."""
    from ..external import OWNER

    if obj.pk is None:
        return True  # Saving it makes this tenant the owner.
    using = obj._state.db or router.db_for_read(type(obj), instance=obj)
    rows = link._base_manager.using(using).filter(record_id=obj.pk, tenant_id=tenant.pk)
    access = rows.values_list("access", flat=True).first()
    if access == OWNER:
        return True
    return (access == WRITE and action in ("view", "change")) or (
        access == READ and action == "view"
    )


class SharedRecordInUse(TenantMismatch):
    """Another tenant's records point at this one, so deleting it would damage their data."""


def _check_not_linked(sender, instance, using, **kwargs) -> None:
    """Runs for cascaded deletes too. Row-level security hides the other tenant's rows from this
    check, and then the database's foreign key refuses the delete instead."""
    config = config_of(sender)
    if config is None or not config.allow_links:
        return
    # The stored owner, as the attribute could be forged.
    stored = sender._base_manager.using(using).filter(pk=instance.pk)
    owner_id = stored.values_list("tenant_id", flat=True).first()
    # include_hidden finds foreign keys declared with related_name="+", as the delete collector does.
    for relation in sender._meta.concrete_model._meta.get_fields(include_hidden=True):
        related = relation.related_model
        if (
            relation.auto_created
            and not relation.concrete
            and (relation.one_to_many or relation.one_to_one)
        ):
            if not getattr(related, "_is_tenant_scoped", False):
                continue
            rows = related._base_manager.using(using).filter(**{relation.field.name: instance})
            if rows.exclude(tenant_id=owner_id).exists():
                raise SharedRecordInUse(
                    f"{instance!r} cannot be deleted while another tenant's records point at it."
                )


class ShareQuerySet(models.QuerySet):
    def _require_owner(self) -> None:
        current = get_current_tenant()
        alias = self._db or router.db_for_write(self.model, **self._hints)
        if current is not None and self.using(alias).exclude(owner_id=current.pk).exists():
            raise TenantMismatch("Only the tenant that owns a record can change its shares.")

    def update(self, **kwargs):
        # Deleting a user clears granted_by through here, which changes no one's access.
        clears_granter = kwargs in ({"granted_by": None}, {"granted_by_id": None})
        if not is_tenant_bypassed():
            if not clears_granter:
                raise TenantMismatch("Use share() and unshare() to change a share.")
            self._require_owner()
        return super().update(**kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        if not is_tenant_bypassed():
            raise TenantMismatch("Use share() to create a share.")
        return super().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, *args, **kwargs):
        if not is_tenant_bypassed():
            raise TenantMismatch("Use share() to change a share.")
        return super().bulk_update(objs, fields, *args, **kwargs)

    def delete(self):
        self._require_owner()
        return super().delete()


class ShareManager(models.Manager.from_queryset(ShareQuerySet)):
    """Shows a tenant the shares it gave and the shares it received."""

    def get_queryset(self):
        queryset = super().get_queryset()
        current = get_current_tenant()
        if current is not None:
            return queryset.filter(Q(owner_id=current.pk) | Q(grantee_id=current.pk))
        if is_tenant_bypassed():
            return queryset
        raise TenantNotSet("No current tenant. Use tenant_context(tenant) or no_tenant().")


class UnscopedShareManager(models.Manager.from_queryset(ShareQuerySet)):
    pass


def _require_scope():
    current = get_current_tenant()
    if current is None and not is_tenant_bypassed():
        raise TenantNotSet("No current tenant. Use tenant_context(tenant) or no_tenant().")
    return current


class AbstractShare(models.Model):
    _tenant_is_share_table = True

    access = models.CharField(max_length=5, choices=ACCESS_CHOICES, default=READ)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ShareManager()
    unscoped_objects = UnscopedShareManager()

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return f"{self.record_id} shared with {self.grantee_id}"

    def save(self, *args, **kwargs):
        if self.access not in (READ, WRITE):
            raise ValueError(f"Unknown access level: {self.access!r}")
        using = kwargs.get("using") or (args[2] if len(args) > 2 else None)
        using = using or router.db_for_write(type(self), instance=self)
        current = _require_scope()
        # Trust only what is stored, since update_fields can leave these attributes unwritten.
        if self.pk is not None:
            stored = type(self)._base_manager.using(using).filter(pk=self.pk)
            stored_owner = stored.values_list("owner_id", flat=True).first()
            if current is not None and stored_owner not in (None, current.pk):
                raise TenantMismatch("Only the tenant that owns a record can change its shares.")
        record_model = self._meta.get_field("record").related_model
        records = record_model._base_manager.using(using).filter(pk=self.record_id)
        owner_id = records.values_list("tenant_id", flat=True).first()
        if owner_id is None or (current is not None and owner_id != current.pk):
            raise TenantMismatch("Only the tenant that owns a record can share it.")
        if self.grantee_id == owner_id:
            raise TenantMismatch("A tenant cannot share a record with itself.")
        self.owner_id = owner_id
        super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        current = _require_scope()
        using = using or router.db_for_write(type(self), instance=self)
        rows = type(self)._base_manager.using(using)
        stored_owner = rows.filter(pk=self.pk).values_list("owner_id", flat=True).first()
        if current is not None and stored_owner != current.pk:
            raise TenantMismatch("Only the tenant that owns a record can end a share.")
        return super().delete(using=using, keep_parents=keep_parents)


def _make_share_model(model):
    tenant_model = getattr(settings, TENANT_MODEL_SETTING)
    name = f"{model.__name__}Share"
    # Django limits index names to 30 characters, so long model names need a short stand-in.
    digest = hashlib.sha256(model._meta.label_lower.encode()).hexdigest()[:16]
    meta = type(
        "Meta",
        (),
        {
            "app_label": model._meta.app_label,
            "base_manager_name": "unscoped_objects",
            "constraints": [
                models.UniqueConstraint(fields=["record", "grantee"], name=f"share_uniq_{digest}"),
                models.CheckConstraint(
                    condition=~Q(owner=F("grantee")),
                    name=f"share_not_self_{digest}",
                ),
            ],
            # Every read of a shared model looks here by grantee.
            "indexes": [
                models.Index(fields=["grantee", "record"], name=f"share_idx_{digest}"),
            ],
        },
    )
    return type(
        name,
        (AbstractShare,),
        {
            "__module__": model.__module__,
            "Meta": meta,
            "record": models.ForeignKey(model, on_delete=models.CASCADE, related_name="shares"),
            "owner": models.ForeignKey(
                tenant_model, on_delete=models.CASCADE, related_name="+", editable=False
            ),
            "grantee": models.ForeignKey(tenant_model, on_delete=models.CASCADE, related_name="+"),
            "granted_by": models.ForeignKey(
                settings.AUTH_USER_MODEL,
                null=True,
                blank=True,
                on_delete=models.SET_NULL,
                related_name="+",
            ),
        },
    )


def _protect_deletes(model) -> None:
    # One sender at a time, as a receiver for every sender would stop Django fast-deleting any
    # model at all, such as expired sessions.
    uid = f"tenant_domains_links_{model._meta.label}"
    models.signals.pre_delete.connect(_check_not_linked, sender=model, dispatch_uid=uid)


@receiver(class_prepared)
def _protect_deletes_through_proxies(sender, **kwargs):
    config = config_of(sender)
    if config is not None and config.allow_links and sender._meta.proxy:
        _protect_deletes(sender)


def shareable(cascade=(), allow_links=False):
    """Class decorator for a TenantModel whose records can be shared with other tenants.

    cascade names reverse relations whose rows inherit each share, such as ["tasks"].
    allow_links lets a grantee's own records keep a foreign key to a record shared with it.
    Run makemigrations afterward, since this generates a <Model>Share table.
    """

    def decorator(model):
        share_model = _make_share_model(model)
        # Lets the generated model be imported like one written by hand, and be pickled.
        setattr(sys.modules[model.__module__], share_model.__name__, share_model)
        model._tenant_sharing = SharingConfig(share_model, tuple(cascade), allow_links)
        if allow_links:
            _protect_deletes(model)
        return model

    return decorator


def share(record, grantee, access=READ, granted_by=None):
    """Grant a tenant access to a record, or change the access it already has."""
    share_model = config_of(type(record)).share_model
    alias = router.db_for_write(share_model, instance=record)
    rows = (
        share_model.objects.using(alias).select_for_update().filter(record=record, grantee=grantee)
    )
    with transaction.atomic(using=alias):
        found = rows.first()
        if found is None:
            created = share_model(record=record, grantee=grantee, access=access)
            created.granted_by = granted_by
            try:
                # A simultaneous call can insert first. Then this one changes that share.
                with transaction.atomic(using=alias):
                    created.save(using=alias)
                return created
            except IntegrityError:
                found = rows.first()
                if found is None:
                    raise
        found.access = access
        found.granted_by = granted_by
        found.save(using=alias)
    return found


def unshare(record, grantee) -> None:
    """End a share. Records of the grantee's that point at this one keep their foreign key."""
    share_model = config_of(type(record)).share_model
    alias = router.db_for_write(share_model, instance=record)
    for found in share_model.objects.using(alias).filter(record=record, grantee=grantee):
        found.delete(using=alias)
