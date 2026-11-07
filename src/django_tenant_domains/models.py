from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, router
from django.db.models.functions import Lower
from django.db.models.signals import class_prepared, m2m_changed
from django.dispatch import receiver
from django.utils import timezone

from .context import get_current_tenant, is_tenant_bypassed
from .exceptions import TenantMismatch, TenantNotSet
from .managers import (
    NO_TENANT_MESSAGE,
    TenantManager,
    UnscopedManager,
    _checked_delete,
    check_foreign_key,
    keeps_unchanged_keys,
    tenant_foreign_keys,
)
from .sharing import has_sharing, writable_q

TENANT_MODEL_SETTING = "TENANT_DOMAINS_TENANT_MODEL"

# Generated migrations refer to settings.TENANT_DOMAINS_TENANT_MODEL, so it must always exist.
if not hasattr(settings, TENANT_MODEL_SETTING):
    setattr(settings, TENANT_MODEL_SETTING, "django_tenant_domains.Tenant")


def get_tenant_model():
    """Return the active Tenant model class, honoring TENANT_DOMAINS_TENANT_MODEL."""
    return apps.get_model(getattr(settings, TENANT_MODEL_SETTING), require_ready=False)


def normalize_slug(slug: str) -> str:
    return slug.strip().lower()


def _save_options(instance, args, kwargs):
    """Return the (using, update_fields) a save will really use, however they were passed."""
    using = args[2] if len(args) > 2 else kwargs.get("using")
    update_fields = args[3] if len(args) > 3 else kwargs.get("update_fields")
    return using or router.db_for_write(type(instance), instance=instance), update_fields


def _touches_field(instance, field_name, update_fields) -> bool:
    """Whether a save(update_fields=...) writes the field, named either way."""
    field = instance._meta.get_field(field_name)
    return field.name in update_fields or field.attname in update_fields


class AbstractTenant(models.Model):
    """Fields every tenant needs. Inherit from this to swap in your own Tenant model."""

    slug = models.SlugField(max_length=63, unique=True)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    timezone = models.CharField(max_length=63, blank=True)
    locale = models.CharField(max_length=35, blank=True)
    restrict_superusers = models.BooleanField(default=False)
    allow_join_requests = models.BooleanField(default=False)
    max_users = models.PositiveIntegerField(
        null=True, blank=True, help_text="Optional cap on active members."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        constraints = [
            # Tenant lookup normalizes the slug, so a stored uppercase slug could never be found.
            models.CheckConstraint(
                condition=models.Q(slug=Lower("slug")),
                name="%(app_label)s_%(class)s_slug_lowercase",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.slug = normalize_slug(self.slug)
        super().save(*args, **kwargs)


class Tenant(AbstractTenant):
    class Meta(AbstractTenant.Meta):
        swappable = TENANT_MODEL_SETTING


class Role(models.Model):
    """A named set of permission codenames. A null tenant means a built-in role."""

    tenant = models.ForeignKey(
        getattr(settings, TENANT_MODEL_SETTING),
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="roles",
    )
    name = models.CharField(max_length=100)
    permissions = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="dtd_role_unique_per_tenant"),
            # NULLs are distinct in a unique constraint, so built-ins need their own.
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(tenant__isnull=True),
                name="dtd_role_unique_built_in",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        super().clean()
        using = self._state.db or router.db_for_write(type(self), instance=self)
        self._check_not_moved_while_in_use(using)

    def save(self, *args, **kwargs):
        using, update_fields = _save_options(self, args, kwargs)
        if update_fields is None or _touches_field(self, "tenant", update_fields):
            self._check_not_moved_while_in_use(using)
        super().save(*args, **kwargs)

    def _check_not_moved_while_in_use(self, using):
        if (
            self.pk is not None
            and self.tenant_id is not None
            and self.memberships.using(using).exclude(tenant_id=self.tenant_id).exists()
        ):
            raise ValidationError({"tenant": "A role in use by another tenant cannot move."})


class Membership(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tenant_memberships"
    )
    tenant = models.ForeignKey(
        getattr(settings, TENANT_MODEL_SETTING),
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    # RESTRICT, not PROTECT: deleting a tenant removes its roles and their memberships together.
    role = models.ForeignKey(Role, on_delete=models.RESTRICT, related_name="memberships")
    is_active = models.BooleanField(default=True)
    last_accessed = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(
        max_length=63, blank=True, help_text="Overrides the tenant's timezone for this member."
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "tenant"], name="dtd_membership_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.tenant}"

    def clean(self):
        super().clean()
        self._check_role_tenant(self.tenant_id, self.role_id, self._state.db)

    def save(self, *args, **kwargs):
        using, update_fields = _save_options(self, args, kwargs)
        tenant_id, role_id = self.tenant_id, self.role_id
        if update_fields is not None:
            # A partial save writes only some fields, so check the pair that ends up stored.
            stored = type(self).objects.using(using).filter(pk=self.pk)
            stored = stored.values_list("tenant_id", "role_id").first()
            if stored is not None:
                if not _touches_field(self, "tenant", update_fields):
                    tenant_id = stored[0]
                if not _touches_field(self, "role", update_fields):
                    role_id = stored[1]
        self._check_role_tenant(tenant_id, role_id, using)
        super().save(*args, **kwargs)

    @staticmethod
    def _check_role_tenant(tenant_id, role_id, using):
        if role_id is None or tenant_id is None:
            return  # The missing field reports its own error.
        # Read the database, since a cached Role may be out of date.
        role_tenant = Role.objects.using(using).filter(pk=role_id).values_list("tenant_id")
        role_tenant = role_tenant.first()
        if role_tenant is not None and role_tenant[0] not in (None, tenant_id):
            raise ValidationError(
                {"role": "A membership's role must be built-in or from its own tenant."}
            )


class JoinRequest(models.Model):
    _tenant_admin_only = True

    class Status(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        DECLINED = "declined"

    tenant = models.ForeignKey(
        getattr(settings, TENANT_MODEL_SETTING),
        on_delete=models.CASCADE,
        related_name="join_requests",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user"],
                condition=models.Q(status="pending"),
                name="dtd_join_request_one_pending",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} to {self.tenant}"


class Invitation(models.Model):
    _tenant_admin_only = True

    class Status(models.TextChoices):
        PENDING = "pending"
        ACCEPTED = "accepted"
        REVOKED = "revoked"

    tenant = models.ForeignKey(
        getattr(settings, TENANT_MODEL_SETTING),
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    email = models.EmailField()
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="+")
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    def __str__(self) -> str:
        return f"{self.email} to {self.tenant}"

    @property
    def is_live(self) -> bool:
        return self.status == self.Status.PENDING and self.expires_at > timezone.now()


class StaffAccessLog(models.Model):
    """One row each time a superuser enters a tenant without being a member of it."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    tenant = models.ForeignKey(
        getattr(settings, TENANT_MODEL_SETTING), on_delete=models.CASCADE, related_name="+"
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    path = models.CharField(max_length=2000)

    def __str__(self) -> str:
        return f"{self.user} in {self.tenant}"


class TenantModel(models.Model):
    """Base for records owned by one tenant.

    `objects` filters by the current tenant. Declare it first if you add managers of your own,
    because Django treats the first manager as the default one.
    """

    _is_tenant_scoped = True

    tenant = models.ForeignKey(
        getattr(settings, TENANT_MODEL_SETTING),
        on_delete=models.PROTECT,
        related_name="+",
    )

    objects = TenantManager()
    unscoped_objects = UnscopedManager()

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        current = get_current_tenant()
        using = self._state.db or router.db_for_write(type(self), instance=self)
        if (
            current is not None
            and self.tenant_id not in (None, current.pk)
            and not self._writable_by(current, using)
        ):
            raise ValidationError("This record would be saved to another tenant.")
        owner_id = self.tenant_id
        if owner_id is None and current is not None:
            owner_id = current.pk
        try:
            self._check_foreign_keys(owner_id, None, using)
        except TenantMismatch as error:
            raise ValidationError(str(error)) from error

    def save(self, *args, **kwargs):
        using, update_fields = _save_options(self, args, kwargs)
        force_insert = args[0] if args else kwargs.get("force_insert", False)
        checked_fields = self._fields_written(update_fields, using, force_insert)
        writes_tenant = checked_fields is None or _touches_field(self, "tenant", checked_fields)
        stored = self._stored_tenant_ids(using)
        current = get_current_tenant()

        if current is not None:
            if stored - {current.pk}:
                # A read-write share lets a grantee edit the record, but it stays the owner's.
                if not self._writable_by(current, using):
                    raise TenantMismatch(f"{self!r} belongs to another tenant.")
                if writes_tenant and self.tenant_id not in stored:
                    raise TenantMismatch(f"{self!r} would be saved to another tenant.")
            elif writes_tenant:
                if self.tenant_id is None:
                    self.tenant_id = current.pk
                elif self.tenant_id != current.pk:
                    raise TenantMismatch(f"{self!r} would be saved to another tenant.")
        elif not is_tenant_bypassed():
            raise TenantNotSet(NO_TENANT_MESSAGE)
        elif writes_tenant and self.tenant_id is None:
            raise TenantNotSet("Set tenant explicitly when creating a record under no_tenant().")

        owner_id = self.tenant_id if writes_tenant else next(iter(stored), None)
        self._check_foreign_keys(owner_id, checked_fields, using)
        super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        using = using or router.db_for_write(type(self), instance=self)
        current = get_current_tenant()
        if current is not None:
            if self._stored_tenant_ids(using) - {current.pk}:
                raise TenantMismatch(f"{self!r} belongs to another tenant.")
        elif not is_tenant_bypassed():
            raise TenantNotSet(NO_TENANT_MESSAGE)
        return _checked_delete(
            type(self),
            using,
            lambda: super(TenantModel, self).delete(using=using, keep_parents=keep_parents),
        )

    def _writable_by(self, tenant, using) -> bool:
        model = type(self)
        if self.pk is None or not has_sharing(model):
            return False
        rows = model._base_manager.using(using).filter(pk=self.pk)
        return rows.filter(writable_q(model, tenant.pk)).exists()

    def _fields_written(self, update_fields, using, force_insert):
        """The fields a save writes. Reading a deferred field would load it and widen the save."""
        if update_fields is not None:
            return update_fields
        deferred = self.get_deferred_fields()
        # Django writes every field on an insert, or when saving to a different database.
        if not deferred or force_insert or using != self._state.db:
            return None
        return {field.attname for field in self._meta.concrete_fields} - deferred

    def _stored_tenant_ids(self, using) -> set:
        """Read the owners from the database, since this instance can be stale or forged.

        With multi-table inheritance the tenant column is on an ancestor, and a save or delete
        acts on that ancestor's row even when the child row does not exist.
        """
        owner_model = self._meta.get_field("tenant").model
        concrete = self._meta.concrete_model
        chain = (concrete, *concrete._meta.get_parent_list())
        # Only the owner model's own key names its row. A child's key can be a field of its own.
        keys = {self.__dict__.get(owner_model._meta.pk.attname)}
        rows = owner_model._base_manager.using(using)
        owners = set()
        # A child's link to its parent can be a different field from its own primary key, and
        # can even point at a field other than the parent's primary key.
        for model in chain:
            for link in model._meta.parents.values():
                value = None if link is None else self.__dict__.get(link.attname)
                if value is None or not issubclass(link.related_model, owner_model):
                    continue
                if link.target_field.primary_key:
                    keys.add(value)
                else:
                    parents = link.related_model._base_manager.using(using)
                    found = parents.filter(**{link.target_field.attname: value})
                    owners |= set(found.values_list("tenant_id", flat=True))
        keys -= {None}
        if keys:
            owners |= set(rows.filter(pk__in=keys).values_list("tenant_id", flat=True))
        return owners

    def _check_foreign_keys(self, owner_id, checked_fields, using) -> None:
        for field in tenant_foreign_keys(type(self)):
            if checked_fields is None or _touches_field(self, field.name, checked_fields):
                value = getattr(self, field.attname)
                if not self._is_kept_link(field, value, using):
                    check_foreign_key(field, value, owner_id, using)

    def clean_fields(self, exclude=None):
        # Django looks up each key's target here, and may not be able to see an unchanged one.
        exclude = set(exclude or ())
        errors = {}
        using = self._state.db or router.db_for_write(type(self), instance=self)
        for field in tenant_foreign_keys(type(self)):
            value = getattr(self, field.attname)
            if (
                field.name not in exclude
                and value is not None
                and self._is_kept_link(field, value, using)
            ):
                exclude.add(field.name)
                try:
                    # All of ForeignKey.clean except the lookup of the target.
                    cleaned = field.to_python(value)
                    models.Field.validate(field, cleaned, self)
                    field.run_validators(cleaned)
                except ValidationError as error:
                    errors[field.name] = error.error_list
        try:
            super().clean_fields(exclude=exclude)
        except ValidationError as error:
            errors.update(error.error_dict)
        if errors:
            raise ValidationError(errors)

    def _is_kept_link(self, field, value, using) -> bool:
        """An unchanged key must not block a save, as its target may no longer be visible.

        A link stays when its share ends, and a grantee cannot see the owner's private records.
        """
        if self.pk is None or not keeps_unchanged_keys(type(self), field):
            return False
        rows = type(self)._base_manager.using(using).filter(pk=self.pk)
        return rows.filter(**{field.attname: value}).exists()


def _stored_owner(instance, using):
    """The owner as stored, since the instance's tenant_id could be forged."""
    rows = type(instance)._base_manager.using(using).filter(pk=instance.pk)
    return rows.values_list("tenant_id", flat=True).first()


def _refuse_links_on_records_not_owned(instance, action, model, pk_set, owner_id, using, current):
    """A share never covers links, from either side of the relation."""
    message = "Only the tenant that owns a record can change its links."
    scoped_instance = isinstance(instance, TenantModel)
    if scoped_instance and owner_id != current.pk:
        raise TenantMismatch(message)
    if has_sharing(model):
        if action == "pre_clear":
            if not scoped_instance:
                # Clearing from an unscoped record would reach other tenants' links.
                raise TenantMismatch(message)
            return
        rows = model._base_manager.using(using).filter(pk__in=pk_set)
        if rows.exclude(tenant_id=current.pk).exists():
            raise TenantMismatch(message)


@receiver(m2m_changed)
def _refuse_cross_tenant_many_to_many(sender, instance, action, model, pk_set, using, **kwargs):
    if action not in ("pre_add", "pre_remove", "pre_clear"):
        return
    scoped_instance = isinstance(instance, TenantModel)
    owner_id = _stored_owner(instance, using) if scoped_instance else None
    current = get_current_tenant()
    if current is not None:
        _refuse_links_on_records_not_owned(
            instance, action, model, pk_set, owner_id, using, current
        )
    if scoped_instance and action == "pre_add" and getattr(model, "_is_tenant_scoped", False):
        rows = model._base_manager.using(using).filter(pk__in=pk_set)
        if owner_id is None or rows.exclude(tenant_id=owner_id).exists():
            raise TenantMismatch("This link would point to another tenant's record.")


@receiver(class_prepared)
def _use_guarded_base_manager(sender, **kwargs):
    # Set here, not in TenantModel.Meta, because a subclass with its own Meta would lose it.
    if issubclass(sender, TenantModel) and not sender._meta.abstract:
        sender._meta.base_manager_name = "unscoped_objects"
