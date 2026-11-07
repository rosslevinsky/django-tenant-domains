from django.db import IntegrityError, connections, models, router, transaction

from .context import get_current_tenant, is_tenant_bypassed
from .exceptions import TenantMismatch, TenantNotSet
from .rls import protected_aliases
from .sharing import (
    SharedRecordInUse,
    can_link,
    config_of,
    has_sharing,
    visible_q,
    writable_q,
)

NO_TENANT_MESSAGE = (
    "No current tenant. Use tenant_context(tenant) or no_tenant(), or unscoped_objects "
    "if you really mean every tenant."
)


def _write_db(queryset):
    """The database a queryset's write goes to, which a router can make differ from reads."""
    return queryset._db or router.db_for_write(queryset.model, **queryset._hints)


def tenant_foreign_keys(model):
    """Yield this model's foreign keys that point at tenant-scoped records."""
    for field in model._meta.concrete_fields:
        if not field.is_relation or field.name == "tenant" or field.remote_field.parent_link:
            continue
        if getattr(field.related_model, "_is_tenant_scoped", False):
            yield field


def check_foreign_key(field, value, owner_id, using) -> None:
    """Foreign key access skips the scoped manager, so a bad key would cross tenants."""
    if value is None or owner_id is None:
        return
    related = field.related_model._base_manager.using(using)
    row = related.filter(**{field.target_field.attname: value}).values_list("tenant_id").first()
    # Row-level security hides another tenant's row, but a foreign key constraint still accepts
    # it. So a row that cannot be seen has to be refused too.
    if row is None:
        raise TenantMismatch(f"{field.name} points to another tenant's record.")
    if row[0] != owner_id and not can_link(field, value, owner_id, using):
        raise TenantMismatch(f"{field.name} points to another tenant's record.")


def keeps_unchanged_keys(model, field) -> bool:
    """Whether a key that is not changing skips its check, as its target may be out of sight."""
    config = config_of(field.related_model)
    return has_sharing(model) or (config is not None and config.allow_links)


def _check_key_values(queryset, owner_id, values) -> None:
    for field in tenant_foreign_keys(queryset.model):
        for name in (field.name, field.attname):
            if name not in values:
                continue
            value = values[name]
            if hasattr(value, "resolve_expression"):
                continue  # Computed in SQL from the row, so there is no value to look up.
            if isinstance(value, models.Model):
                value = getattr(value, field.target_field.attname)
            alias = _write_db(queryset)
            if (
                keeps_unchanged_keys(queryset.model, field)
                and not queryset.using(alias).exclude(**{field.attname: value}).exists()
            ):
                continue
            check_foreign_key(field, value, owner_id, alias)


def _check_objects_keys(queryset, objs, fields=None) -> None:
    seen = set()
    current = get_current_tenant()
    # In an update, a key must suit the stored owner. The object's own tenant_id could be forged.
    stored = {}
    if current is not None and fields is not None and has_sharing(queryset.model):
        rows = queryset.model._base_manager.using(_write_db(queryset))
        pks = [obj.pk for obj in objs]
        stored = dict(rows.filter(pk__in=pks).values_list("pk", "tenant_id"))
    for field in tenant_foreign_keys(queryset.model):
        if fields is not None and field.name not in fields and field.attname not in fields:
            continue
        kept = {}
        if fields is not None and keeps_unchanged_keys(queryset.model, field):
            rows = queryset.model._base_manager.using(_write_db(queryset))
            pks = [obj.pk for obj in objs]
            kept = dict(rows.filter(pk__in=pks).values_list("pk", field.attname))
        for obj in objs:
            if obj.pk in kept and kept[obj.pk] == getattr(obj, field.attname):
                continue
            owner_id = obj.tenant_id if current is None else stored.get(obj.pk, current.pk)
            key = (field.name, getattr(obj, field.attname), owner_id)
            if key not in seen:
                seen.add(key)
                check_foreign_key(field, key[1], owner_id, _write_db(queryset))


def _owner_for_update(queryset, current, values) -> int:
    """A foreign key must suit the row's owner, which on a shared row is not the current tenant."""
    names = {n for f in tenant_foreign_keys(queryset.model) for n in (f.name, f.attname)}
    # Computed in SQL from each row, as bulk_update does, so there is no one value to check.
    direct = {name for name, value in values.items() if not hasattr(value, "resolve_expression")}
    if not has_sharing(queryset.model) or not names & direct:
        return current.pk
    rows = queryset.using(_write_db(queryset)).order_by()
    owners = set(rows.values_list("tenant_id", flat=True).distinct())
    if len(owners) > 1:
        raise TenantMismatch("This update spans records with different owners.")
    return owners.pop() if owners else current.pk


def _refuse_changing_primary_key(model, values) -> None:
    primary = model._meta.pk
    if has_sharing(model) and {"pk", primary.name, primary.attname} & set(values):
        raise TenantMismatch("A shared record's primary key cannot be changed.")


def _refuse_changing_tenant() -> None:
    if not is_tenant_bypassed():
        raise TenantMismatch("Only no_tenant() may change which tenant owns a record.")


def _refuse_unwritable_rows(queryset) -> None:
    """Reads include rows shared with this tenant, but a write needs read-write access to them."""
    current = get_current_tenant()
    if (
        current is not None
        and has_sharing(queryset.model)
        and queryset.using(_write_db(queryset))
        .exclude(writable_q(queryset.model, current.pk))
        .exists()
    ):
        raise TenantMismatch("This write would change a record shared as read-only.")


def _checked_delete(model, using, delete):
    """Row-level security hides the rows pointing at this one, so only the database knows."""
    config = config_of(model)
    if config is None or not config.allow_links or not protected_aliases():
        return delete()
    try:
        with transaction.atomic(using=using):
            result = delete()
            connections[using].check_constraints()
    except IntegrityError as error:
        raise SharedRecordInUse(
            "This record cannot be deleted while another tenant's records point at it."
        ) from error
    return result


class TenantQuerySet(models.QuerySet):
    def owned(self):
        """Only this tenant's own records, leaving out those shared with it."""
        current = get_current_tenant()
        return self if current is None else self.filter(tenant_id=current.pk)

    def delete(self):
        current = get_current_tenant()
        if (
            current is not None
            and has_sharing(self.model)
            and self.using(_write_db(self)).exclude(tenant_id=current.pk).exists()
        ):
            raise TenantMismatch("Only the tenant that owns a record can delete it.")
        return _checked_delete(self.model, _write_db(self), super().delete)

    def update(self, **kwargs):
        if "tenant" in kwargs or "tenant_id" in kwargs:
            _refuse_changing_tenant()
        current = get_current_tenant()
        if current is not None:
            _refuse_changing_primary_key(self.model, kwargs)
            _refuse_unwritable_rows(self)
            _check_key_values(self, _owner_for_update(self, current, kwargs), kwargs)
        return super().update(**kwargs)

    def update_or_create(self, defaults=None, create_defaults=None, **kwargs):
        # Postgres applies the UPDATE policy to the row lock this takes, so under row-level
        # security a record shared read-only goes unseen and a second record would be created.
        _refuse_unwritable_rows(self.filter(**kwargs))
        return super().update_or_create(defaults, create_defaults, **kwargs)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        objs = list(objs)
        current = get_current_tenant()
        if current is not None and update_conflicts:
            # The conflicting row may be one this tenant cannot see or may only read.
            if has_sharing(self.model):
                raise TenantMismatch("update_conflicts is not available on shared models.")
            if {"tenant", "tenant_id"} & set(update_fields or ()):
                _refuse_changing_tenant()
        for obj in objs:
            if current is None:
                continue
            if obj.tenant_id is None:
                obj.tenant_id = current.pk
            elif obj.tenant_id != current.pk:
                raise TenantMismatch(f"{obj!r} belongs to another tenant.")
        _check_objects_keys(self, objs)
        return super().bulk_create(
            objs, batch_size, ignore_conflicts, update_conflicts, update_fields, unique_fields
        )

    def bulk_update(self, objs, fields, *args, **kwargs):
        if "tenant" in fields or "tenant_id" in fields:
            _refuse_changing_tenant()
        objs = list(objs)
        current = get_current_tenant()
        if current is not None:
            # This queryset's own rows, as a filter like owned() leaves the rest untouched.
            reached = self.filter(pk__in=[obj.pk for obj in objs])
            _refuse_unwritable_rows(reached)
            if has_sharing(self.model):
                keys = set(reached.using(_write_db(self)).values_list("pk", flat=True))
                _check_objects_keys(self, [obj for obj in objs if obj.pk in keys], fields)
                return super().bulk_update(objs, fields, *args, **kwargs)
        _check_objects_keys(self, objs, fields)
        return super().bulk_update(objs, fields, *args, **kwargs)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Filters every query by the current tenant, and refuses to run without one."""

    def get_queryset(self):
        queryset = super().get_queryset()
        tenant = get_current_tenant()
        if tenant is not None:
            if has_sharing(self.model):
                return queryset.filter(visible_q(self.model, tenant.pk))
            return queryset.filter(tenant=tenant)
        if is_tenant_bypassed():
            return queryset
        raise TenantNotSet(NO_TENANT_MESSAGE)


class UnscopedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # Reverse foreign key add() and set() update through the base manager, this one.
        current = get_current_tenant()
        if current is None:
            return super().update(**kwargs)
        if "tenant" in kwargs or "tenant_id" in kwargs:
            _refuse_changing_tenant()
        if has_sharing(self.model):
            # The same rules as TenantQuerySet, for add() and set() on a reverse foreign key.
            _refuse_changing_primary_key(self.model, kwargs)
            _refuse_unwritable_rows(self)
            _check_key_values(self, _owner_for_update(self, current, kwargs), kwargs)
            return super().update(**kwargs)
        if self.using(_write_db(self)).exclude(tenant=current).exists():
            raise TenantMismatch("This update would change another tenant's records.")
        _check_key_values(self, current.pk, kwargs)
        return super().update(**kwargs)


class UnscopedManager(models.Manager.from_queryset(UnscopedQuerySet)):
    """Reads every tenant. Django also uses it as the base manager of a TenantModel."""
