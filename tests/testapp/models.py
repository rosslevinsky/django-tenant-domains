from django.conf import settings
from django.contrib.auth.models import User
from django.db import models

from django_tenant_domains.external import register_external
from django_tenant_domains.models import TenantModel
from django_tenant_domains.sharing import shareable
from django_tenant_domains.users import TenantPermissionsMixin
from tests.thirdparty.models import Article, Book, Code, Snippet


class Tag(TenantModel):
    name = models.CharField(max_length=50)

    class Meta:
        verbose_name = "label"


class Project(TenantModel):
    name = models.CharField(max_length=50)
    tags = models.ManyToManyField(Tag, blank=True)

    class Meta:
        ordering = ["pk"]


class Task(TenantModel):
    title = models.CharField(max_length=50)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    reviewer = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )


class SpecialProject(Project):
    """Multi-table inheritance: the tenant column is on the parent's table."""

    level = models.IntegerField(default=0)


class CustomChild(Project):
    """A child with a primary key of its own, separate from the link to its parent."""

    child_id = models.IntegerField(primary_key=True)
    project_ptr = models.OneToOneField(Project, on_delete=models.CASCADE, parent_link=True)


class CodedParent(TenantModel):
    code = models.IntegerField(unique=True)


class CodedChild(CodedParent):
    """A child linked to its parent by a unique field, not by the parent's primary key."""

    parent = models.OneToOneField(
        CodedParent, on_delete=models.CASCADE, parent_link=True, to_field="code"
    )


class LegacyNote(TenantModel):
    """A table this project's migrations do not create."""

    text = models.CharField(max_length=50)

    class Meta:
        managed = False
        db_table = "testapp_legacynote"


@shareable(cascade=["files"], allow_links=True)
class Folder(TenantModel):
    """Shared with cascade, and other tenants' records may point at it."""

    name = models.CharField(max_length=50)
    tags = models.ManyToManyField(Tag, blank=True)


class File(TenantModel):
    """Inherits shares from its folder."""

    title = models.CharField(max_length=50)
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name="files")


class FileProxy(File):
    class Meta:
        proxy = True


class Card(TenantModel):
    """Does not inherit shares, but can link to a folder shared with its tenant."""

    title = models.CharField(max_length=50)
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name="cards")


@shareable()
class Album(TenantModel):
    """Shared with no cascade and no links."""

    name = models.CharField(max_length=50)


class Photo(TenantModel):
    title = models.CharField(max_length=50)
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="photos")


@shareable()
class Notebook(TenantModel):
    """Linked to an unscoped model, so links can be changed from the unscoped side."""

    title = models.CharField(max_length=50)
    editors = models.ManyToManyField(User, related_name="notebooks", blank=True)


@shareable(cascade=["pages"])
class Binder(TenantModel):
    name = models.CharField(max_length=50)


@shareable(allow_links=True)
class Page(TenantModel):
    """Inherits shares from its binder, and other tenants' records may link to it."""

    binder = models.ForeignKey(Binder, on_delete=models.CASCADE, related_name="pages")


class PageNote(TenantModel):
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="notes")


@shareable()
class Sheet(TenantModel):
    """Shared directly, with a foreign key to a record that stays private."""

    title = models.CharField(max_length=50)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="sheets")


class FolderProxy(Folder):
    class Meta:
        proxy = True


class HiddenLink(TenantModel):
    """Points at a shared record with no reverse accessor."""

    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name="+")


@shareable(allow_links=True)
class KeyedParent(TenantModel):
    code = models.IntegerField(unique=True)


class KeyedChild(TenantModel):
    parent = models.ForeignKey(
        KeyedParent, to_field="code", on_delete=models.CASCADE, related_name="children"
    )


class KeyedLink(TenantModel):
    parent = models.ForeignKey(
        KeyedParent, to_field="code", on_delete=models.CASCADE, related_name="+"
    )


class ShieldedUser(TenantPermissionsMixin, User):
    """Stands in for a project's own user model."""

    class Meta:
        proxy = True


class Comment(models.Model):
    """Not tenant-scoped, but points at a scoped record."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)


class Review(models.Model):
    """Registered as external, with keys to a tenant's record and to a third-party record."""

    tag = models.ForeignKey(Tag, null=True, on_delete=models.CASCADE, related_name="+")
    article = models.ForeignKey(Article, null=True, on_delete=models.CASCADE, related_name="+")


ArticleTenantLink = register_external(Article)
SnippetTenantLink = register_external(Snippet)
CodeTenantLink = register_external(Code)
BookTenantLink = register_external(Book)
ReviewTenantLink = register_external(Review)
