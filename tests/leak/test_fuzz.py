"""Random operations as one tenant, checked against what is really stored.

A Hypothesis state machine gives three tenants records across the test models, shares some of them,
and then acts as one tenant at a time, inside tenant_context or through a real request. After every
step it reads every table through the login that bypasses row-level security, and checks that:

- a read returned exactly what the acting tenant may see, counted the same way,
- a write created, changed or deleted only rows the acting tenant may write, and
- the stored data still keeps every foreign key and share inside the rules.

Row-level security also binds joins and raw SQL on Postgres. On SQLite only the ORM filter applies,
so a join there reaches every tenant's rows, as docs/known-gaps.md describes, and the expected
results say so. Raw SQL and third-party managers run on Postgres only, for the same reason.

Left out, as docs/known-gaps.md lists them: unscoped_objects, no_tenant(), F() and other
expressions in update(), bulk_create(update_conflicts=True), raw SQL that writes foreign keys,
through-model writes, refresh_from_db and deferred fields, querysets kept across tenants, and
joins through unscoped tables.

DTD_FUZZ_EXAMPLES sets the number of examples (default 50), and DTD_FUZZ_STEPS the steps in each
(default 25).
"""

import os
from collections import Counter
from dataclasses import dataclass

import pytest
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import DatabaseError, connection, connections, transaction
from django.db.models import Count, Exists, Max, OuterRef, Prefetch, Q
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import path
from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test

from django_tenant_domains import external
from django_tenant_domains.access import Outcome
from django_tenant_domains.context import tenant_context
from django_tenant_domains.exceptions import TenantMismatch
from django_tenant_domains.middleware import TenantMiddleware
from django_tenant_domains.models import Tenant
from django_tenant_domains.sharing import READ, WRITE, share, unshare
from tests.support import ALL_DATABASES, BYPASS, POSTGRES
from tests.testapp.models import (
    Album,
    AlbumShare,
    ArticleTenantLink,
    Card,
    File,
    Folder,
    FolderShare,
    Photo,
    Project,
    Sheet,
    SheetShare,
    Tag,
    Task,
)
from tests.thirdparty.models import Article

pytestmark = pytest.mark.django_db(transaction=True, databases=ALL_DATABASES)

EXAMPLES = int(os.environ.get("DTD_FUZZ_EXAMPLES", "50"))
STEPS = int(os.environ.get("DTD_FUZZ_STEPS", "25"))
SLUGS = ["acme", "globex", "initech"]
# Few names, so filters by name match other tenants' rows too.
WORDS = ["a", "b", "c"]
# An action that was refused. Anything else escapes and fails the test.
REFUSED = (TenantMismatch, DatabaseError, ObjectDoesNotExist)
# How often each step succeeded or was refused, shown with pytest -s.
OUTCOMES = Counter()


@dataclass(frozen=True)
class Spec:
    model: type
    text: str
    fks: tuple = ()  # (field name, target label, nullable)
    share: str | None = None  # Label of the share table.
    cascade: tuple = ()  # (field name, parent label) through which the parent's shares reach it.
    links: bool = False  # Other tenants' records may point at it.


SCOPED = {
    "tag": Spec(Tag, "name"),
    "project": Spec(Project, "name"),
    "task": Spec(Task, "title", fks=(("project", "project", False), ("reviewer", "project", True))),
    "folder": Spec(Folder, "name", share="folder_share", links=True),
    "file": Spec(
        File, "title", fks=(("folder", "folder", False),), cascade=(("folder", "folder"),)
    ),
    "card": Spec(Card, "title", fks=(("folder", "folder", False),)),
    "album": Spec(Album, "name", share="album_share"),
    "photo": Spec(Photo, "title", fks=(("album", "album", False),)),
    "sheet": Spec(Sheet, "title", fks=(("tag", "tag", False),), share="sheet_share"),
}
SHARES = {
    "folder_share": (FolderShare, "folder"),
    "album_share": (AlbumShare, "album"),
    "sheet_share": (SheetShare, "sheet"),
}
SHARED_LABEL = {record: label for label, (_, record) in SHARES.items()}
THROUGHS = {
    "project_tags": (Project.tags.through, "project", "tag"),
    "folder_tags": (Folder.tags.through, "folder", "tag"),
}
OTHER = {"article": Article, "article_link": ArticleTenantLink, "tenant": Tenant}
NO_KEYS = [label for label, spec in SCOPED.items() if not spec.fks]


def _columns(label):
    if label in SCOPED:
        spec = SCOPED[label]
        return ["tenant_id", spec.text, *(f"{name}_id" for name, _, _ in spec.fks)]
    if label in SHARES:
        return ["record_id", "owner_id", "grantee_id", "access"]
    if label in THROUGHS:
        _, left, right = THROUGHS[label]
        return [f"{left}_id", f"{right}_id"]
    return {"article": ["title"], "article_link": ["record_id", "tenant_id", "access"]}.get(
        label, ["slug", "name"]
    )


def _model(label):
    if label in SCOPED:
        return SCOPED[label].model
    if label in SHARES:
        return SHARES[label][0]
    if label in THROUGHS:
        return THROUGHS[label][0]
    return OTHER[label]


LABELS = [*SCOPED, *SHARES, *THROUGHS, *OTHER]


def snapshot():
    """Every row of every table, read through the login that sees all tenants."""
    out = {}
    for label in LABELS:
        rows = _model(label)._base_manager.using(BYPASS).values("id", *_columns(label))
        out[label] = {row["id"]: row for row in rows}
    return out


@dataclass(frozen=True)
class Rel:
    path: str  # The query name.
    kind: str  # forward, reverse or m2m.
    other: str
    fk: str = ""  # The foreign key: on the base model if forward, on the other if reverse.
    through: str = ""
    accessor: str = ""  # The manager on an instance, for reverse and m2m.
    nullable: bool = False


RELATIONS = {
    "tag": [
        Rel("project", "m2m", "project", through="project_tags", accessor="project_set"),
        Rel("folder", "m2m", "folder", through="folder_tags", accessor="folder_set"),
        Rel("sheets", "reverse", "sheet", fk="tag", accessor="sheets"),
    ],
    "project": [
        Rel("tasks", "reverse", "task", fk="project", accessor="tasks"),
        Rel("tags", "m2m", "tag", through="project_tags", accessor="tags"),
    ],
    "task": [
        Rel("project", "forward", "project", fk="project"),
        Rel("reviewer", "forward", "project", fk="reviewer", nullable=True),
    ],
    "folder": [
        Rel("files", "reverse", "file", fk="folder", accessor="files"),
        Rel("cards", "reverse", "card", fk="folder", accessor="cards"),
        Rel("tags", "m2m", "tag", through="folder_tags", accessor="tags"),
    ],
    "file": [Rel("folder", "forward", "folder", fk="folder")],
    "card": [Rel("folder", "forward", "folder", fk="folder")],
    "album": [Rel("photos", "reverse", "photo", fk="album", accessor="photos")],
    "photo": [Rel("album", "forward", "album", fk="album")],
    "sheet": [Rel("tag", "forward", "tag", fk="tag")],
}


class View:
    """What one tenant may see and change in one snapshot, by the rules in the docs."""

    def __init__(self, snap, tenant_pk):
        self.s = snap
        self.t = tenant_pk
        self.grants = {}
        for label in SHARES:
            for row in snap[label].values():
                self.grants[label, row["record_id"], row["grantee_id"]] = row["access"]

    def _granted(self, share_label, record, write):
        access = self.grants.get((share_label, record, self.t))
        return access is not None and (not write or access == WRITE)

    def can(self, label, row, write=False):
        spec = SCOPED[label]
        if row["tenant_id"] == self.t:
            return True
        if spec.share and self._granted(spec.share, row["id"], write):
            return True
        for name, parent in spec.cascade:
            found = self.s[parent].get(row[f"{name}_id"])
            # Only the parent owner's own children inherit its shares.
            if (
                found is not None
                and found["tenant_id"] == row["tenant_id"]
                and self._granted(SCOPED[parent].share, found["id"], write)
            ):
                return True
        return False

    def owns(self, label, row):
        return row["tenant_id"] == self.t

    def visible(self, label):
        return [row for _, row in sorted(self.s[label].items()) if self.can(label, row)]

    def lockable(self, label):
        """What select_for_update() returns. Postgres also applies the update policy to it, so a
        record shared read-only drops out: see tests/leak/test_fuzz_findings.py."""
        if not POSTGRES:
            return self.visible(label)
        return [row for row in self.visible(label) if self.can(label, row, write=True)]

    def joined(self, label, row):
        """Whether a join reaches the row. Only row-level security filters joined tables."""
        if not POSTGRES:
            return True
        if label in SCOPED:
            return self.can(label, row)
        if label in THROUGHS:
            _, left, right = THROUGHS[label]
            return self.joined(left, self.s[left][row[f"{left}_id"]]) and self.joined(
                right, self.s[right][row[f"{right}_id"]]
            )
        if label in SHARES:
            return self.t in (row["owner_id"], row["grantee_id"])
        if label == "article":
            return bool(self.article_access(row["id"]))
        raise AssertionError(label)

    def related(self, label, rel, row, scoped=False):
        """The rows a relation reaches from a row. scoped: through the other model's manager."""
        reach = self.can if scoped else self.joined
        other = self.s[rel.other]
        if rel.kind == "forward":
            found = other.get(row[f"{rel.fk}_id"])
            return [found] if found is not None and reach(rel.other, found) else []
        if rel.kind == "reverse":
            return [
                child
                for _, child in sorted(other.items())
                if child[f"{rel.fk}_id"] == row["id"] and reach(rel.other, child)
            ]
        found = []
        for _, link in sorted(self.s[rel.through].items()):
            if link[f"{label}_id"] != row["id"]:
                continue
            target = other[link[f"{rel.other}_id"]]
            if self.joined(rel.through, link) and reach(rel.other, target):
                found.append(target)
        return found

    def article_access(self, pk):
        return {
            row["access"]
            for row in self.s["article_link"].values()
            if row["record_id"] == pk and row["tenant_id"] == self.t
        }

    def article_owner(self, pk):
        for row in self.s["article_link"].values():
            if row["record_id"] == pk and row["access"] == "owner":
                return row["tenant_id"]
        return None


# Reads


FILTERS = ["all", "text", "pk_in", "tenant", "or", "exclude", "related", "subquery", "exists"]


@dataclass(frozen=True)
class Filter:
    kind: str
    word: str
    pks: tuple
    tenant: int
    rel: Rel | None

    def apply(self, label):
        spec = SCOPED[label]
        rows = spec.model.objects.all()
        text = spec.text
        if self.kind == "text":
            return rows.filter(**{text: self.word})
        if self.kind == "pk_in":
            return rows.filter(pk__in=self.pks)
        if self.kind == "tenant":
            return rows.filter(tenant_id=self.tenant)
        if self.kind == "or":
            return rows.filter(Q(pk__in=self.pks) | Q(**{text: self.word}))
        if self.kind == "exclude":
            return rows.exclude(**{text: self.word})
        if self.kind == "all":
            return rows
        rel = self.rel
        other = SCOPED[rel.other]
        match = {other.text: self.word}
        if self.kind == "related":
            return rows.filter(**{f"{rel.path}__{other.text}": self.word})
        if self.kind == "subquery" and rel.kind == "reverse":
            return rows.filter(pk__in=other.model.objects.filter(**match).values(rel.fk))
        if self.kind == "subquery":
            return rows.filter(
                **{f"{rel.fk}__in": other.model.objects.filter(**match).values("pk")}
            )
        if rel.kind == "reverse":
            inner = other.model.objects.filter(**{rel.fk: OuterRef("pk")}, **match)
        else:
            inner = other.model.objects.filter(pk=OuterRef(f"{rel.fk}_id"), **match)
        return rows.filter(Exists(inner))

    def times(self, view, label, row):
        """How many times the filtered query returns the row, joins included."""
        text = SCOPED[label].text
        if self.kind in ("all", "text", "pk_in", "tenant", "or", "exclude"):
            return int(
                {
                    "all": True,
                    "text": row[text] == self.word,
                    "pk_in": row["id"] in self.pks,
                    "tenant": row["tenant_id"] == self.tenant,
                    "or": row["id"] in self.pks or row[text] == self.word,
                    "exclude": row[text] != self.word,
                }[self.kind]
            )
        other_text = SCOPED[self.rel.other].text
        if self.kind == "related":
            found = view.related(label, self.rel, row)
            return sum(1 for other in found if other[other_text] == self.word)
        # A subquery runs through the other model's manager, so it sees what that manager does.
        found = view.related(label, self.rel, row, scoped=True)
        return int(any(other[other_text] == self.word for other in found))

    def expected_ids(self, view, label, locked=False):
        ids = []
        for row in view.lockable(label) if locked else view.visible(label):
            ids += [row["id"]] * self.times(view, label, row)
        return ids


SHAPES = [
    "instances",
    "iterator",
    "values",
    "values_list",
    "count",
    "exists",
    "aggregate",
    "in_bulk",
    "first",
    "union",
    "locked",
    "latest",
    "distinct",
]


def run_shape(rows, shape, label, word):
    spec = SCOPED[label]
    if shape == "instances":
        return sorted(row.pk for row in rows)
    if shape == "iterator":
        return sorted(row.pk for row in rows.iterator(chunk_size=2))
    if shape == "values":
        return sorted(row["id"] for row in rows.values("id", spec.text))
    if shape == "values_list":
        return sorted(rows.values_list("id", "tenant_id", spec.text))
    if shape == "count":
        return rows.count()
    if shape == "exists":
        return rows.exists()
    if shape == "aggregate":
        return rows.aggregate(n=Count("id"), top=Max("id"))
    if shape == "in_bulk":
        return sorted(rows.in_bulk())
    if shape == "first":
        found = rows.order_by("pk").first()
        return None if found is None else found.pk
    if shape == "locked":
        return sorted(row.pk for row in rows.select_for_update(of=("self",)))
    if shape == "latest":
        try:
            return rows.latest("pk").pk
        except ObjectDoesNotExist:
            return None
    if shape == "distinct":
        return sorted(rows.distinct().values_list("id", flat=True))
    others = spec.model.objects.filter(**{spec.text: word}).order_by().values_list("id", flat=True)
    return sorted(rows.order_by().values_list("id", flat=True).union(others))


def expect_shape(view, ids, shape, label, word):
    spec = SCOPED[label]
    rows = view.s[label]
    if shape in ("instances", "iterator", "values", "locked"):
        return sorted(ids)
    if shape == "values_list":
        return sorted((pk, rows[pk]["tenant_id"], rows[pk][spec.text]) for pk in ids)
    if shape == "count":
        return len(ids)
    if shape == "exists":
        return bool(ids)
    if shape == "aggregate":
        return {"n": len(ids), "top": max(ids, default=None)}
    if shape == "in_bulk":
        return sorted(set(ids))
    if shape == "first":
        return min(ids, default=None)
    if shape == "latest":
        return max(ids, default=None)
    if shape == "distinct":
        return sorted(set(ids))
    extra = {row["id"] for row in view.visible(label) if row[spec.text] == word}
    return sorted(set(ids) | extra)


RELATED_READS = [
    "annotate",
    "aggregate",
    "values_across",
    "group_by",
    "select_related",
    "prefetch",
    "prefetch_filtered",
    "fk_access",
    "manager",
]


def _key(value):
    return repr(value)


def run_related(label, rel, kind, held):
    spec = SCOPED[label]
    rows = spec.model.objects.all()
    other_text = SCOPED[rel.other].text
    if kind == "annotate":
        return sorted(rows.annotate(n=Count(rel.path)).values_list("id", "n"))
    if kind == "aggregate":
        return rows.aggregate(n=Count(rel.path))["n"]
    if kind == "group_by":
        groups = rows.values_list(f"{rel.path}__{other_text}").annotate(n=Count("id"))
        return sorted(groups.order_by(), key=_key)
    if kind == "prefetch_filtered":
        other = SCOPED[rel.other].model.objects.filter(**{other_text: "a"})
        found = []
        for row in rows.prefetch_related(Prefetch(rel.accessor, queryset=other)):
            found.append((row.pk, sorted(o.pk for o in getattr(row, rel.accessor).all())))
        return sorted(found, key=_key)
    if kind == "values_across":
        return sorted(rows.values_list("id", f"{rel.path}__{other_text}"), key=_key)
    if kind == "select_related":
        found = []
        for row in rows.select_related(rel.path):
            target = getattr(row, rel.path)
            found.append((row.pk, getattr(row, f"{rel.fk}_id"), target and target.pk))
        return sorted(found, key=_key)
    if kind == "prefetch":
        found = []
        for row in rows.prefetch_related(rel.accessor or rel.path):
            if rel.kind == "forward":
                try:
                    target = getattr(row, rel.path)
                except ObjectDoesNotExist:
                    found.append((row.pk, "missing"))
                else:
                    found.append((row.pk, target and target.pk))
            else:
                found.append((row.pk, sorted(o.pk for o in getattr(row, rel.accessor).all())))
        return sorted(found, key=_key)
    if kind == "fk_access":
        found = []
        for row in rows:
            try:
                target = getattr(row, rel.path)
            except ObjectDoesNotExist:
                found.append((row.pk, "missing"))
            else:
                found.append((row.pk, target and target.pk))
        return sorted(found, key=_key)
    if held is not None:
        # The manager of a record held from elsewhere, possibly another tenant's.
        return sorted(getattr(held, rel.accessor).values_list("pk", flat=True))
    return sorted(
        (row.pk, sorted(getattr(row, rel.accessor).values_list("pk", flat=True))) for row in rows
    )


def expect_related(view, label, rel, kind, held_row):
    other_text = SCOPED[rel.other].text
    found = []
    if kind == "manager" and held_row is not None:
        return sorted(o["id"] for o in view.related(label, rel, held_row, scoped=True))
    if kind == "aggregate":
        return sum(len(view.related(label, rel, row)) for row in view.visible(label))
    if kind == "group_by":
        across = expect_related(view, label, rel, "values_across", None)
        counts = Counter(text for _, text in across)
        return sorted(((text,) + (n,) for text, n in counts.items()), key=_key)
    for row in view.visible(label):
        joined = view.related(label, rel, row)
        target = joined[0]["id"] if joined else None
        if kind == "annotate":
            found.append((row["id"], len(joined)))
        elif kind == "values_across":
            if joined:
                found += [(row["id"], other[other_text]) for other in joined]
            elif rel.kind != "forward" or rel.nullable:
                found.append((row["id"], None))  # A left outer join.
        elif kind == "select_related":
            if joined or rel.nullable:
                found.append((row["id"], row[f"{rel.fk}_id"], target))
        elif kind == "prefetch" and rel.kind == "forward":
            # Forward prefetching uses the base manager, so only row-level security filters it.
            # A required key whose target it hides raises DoesNotExist, as plain access does.
            missing = row[f"{rel.fk}_id"] is not None and not joined and not rel.nullable
            found.append((row["id"], "missing" if missing else target))
        elif kind == "prefetch" or kind == "manager":
            scoped = view.related(label, rel, row, scoped=True)
            found.append((row["id"], sorted(o["id"] for o in scoped)))
        elif kind == "prefetch_filtered":
            scoped = view.related(label, rel, row, scoped=True)
            found.append((row["id"], sorted(o["id"] for o in scoped if o[other_text] == "a")))
        elif kind == "fk_access":
            if row[f"{rel.fk}_id"] is None:
                found.append((row["id"], None))
            else:
                found.append((row["id"], target if joined else "missing"))
    return sorted(found, key=_key)


# Checking writes


def _fk_allowed(snaps, label, row, old=None):
    """A new or changed foreign key stays in its row's tenant, or links to a record shared with
    that tenant. An unchanged one may outlive its share, as docs/sharing.md says."""
    for name, target, _ in SCOPED[label].fks:
        value = row[f"{name}_id"]
        if value is None or (old is not None and old[f"{name}_id"] == value):
            continue
        found = [s[target][value] for s in snaps if value in s[target]]
        assert found, f"{label} {row['id']} points at a missing {target}"
        if found[0]["tenant_id"] == row["tenant_id"]:
            continue
        shared = SCOPED[target].links and any(
            View(s, row["tenant_id"]).can(target, s[target][value])
            for s in snaps
            if value in s[target]
        )
        assert shared, f"{label} {row['id']} points at another tenant's {target} {value}"


def _owner(snaps, label, pk):
    for s in snaps:
        if pk in s[label]:
            return s[label][pk]["tenant_id"]
    raise AssertionError(f"no {label} {pk}")


def check_write(before, after, actor):
    """Only rows the actor may write were created, changed or deleted."""
    view = View(before, actor)
    snaps = (before, after)
    assert after["tenant"] == before["tenant"], "tenants changed"
    for label in SCOPED:
        old, new = before[label], after[label]
        for pk in old.keys() - new.keys():
            assert view.owns(label, old[pk]), (
                f"deleted {label} {pk} of tenant {old[pk]['tenant_id']}"
            )
        for pk in new.keys() - old.keys():
            assert new[pk]["tenant_id"] == actor, f"created {label} {pk} for another tenant"
            _fk_allowed(snaps, label, new[pk])
        for pk in old.keys() & new.keys():
            if old[pk] == new[pk]:
                continue
            assert view.can(label, old[pk], write=True), (
                f"changed {label} {pk} without write access"
            )
            assert old[pk]["tenant_id"] == new[pk]["tenant_id"], f"moved {label} {pk}"
            _fk_allowed(snaps, label, new[pk], old[pk])
    for label, (_, left, right) in THROUGHS.items():
        old, new = before[label], after[label]
        for pk in old.keys() ^ new.keys():
            row = old.get(pk) or new[pk]
            owners = {
                _owner(snaps, left, row[f"{left}_id"]),
                _owner(snaps, right, row[f"{right}_id"]),
            }
            assert owners == {actor}, f"changed a {label} link of tenants {owners}"
        for pk in old.keys() & new.keys():
            assert old[pk] == new[pk], f"rewrote {label} {pk}"
    for label, (_, record) in SHARES.items():
        old, new = before[label], after[label]
        for pk in old.keys() | new.keys():
            if old.get(pk) == new.get(pk):
                continue
            for row in (old.get(pk), new.get(pk)):
                if row is not None:
                    assert row["owner_id"] == actor, f"changed {label} {pk} owned by another"
                    assert _owner(snaps, record, row["record_id"]) == actor
    check_articles(before, after, actor)


def check_articles(before, after, actor):
    old_view, new_view = View(before, actor), View(after, actor)
    old, new = before["article"], after["article"]
    for pk in old.keys() - new.keys():
        assert old_view.article_owner(pk) == actor, f"deleted article {pk} of another tenant"
    for pk in new.keys() - old.keys():
        assert new_view.article_owner(pk) == actor, f"created article {pk} not owned by actor"
    for pk in old.keys() & new.keys():
        if old[pk] != new[pk]:
            access = old_view.article_access(pk)
            assert access & {"owner", WRITE}, f"changed article {pk} without write access"
    old_links, new_links = before["article_link"], after["article_link"]
    for pk in old_links.keys() | new_links.keys():
        if old_links.get(pk) == new_links.get(pk):
            continue
        row = old_links.get(pk) or new_links[pk]
        record = row["record_id"]
        owner = old_view.article_owner(record) or new_view.article_owner(record)
        assert owner == actor, f"changed a link of article {record}, owned by {owner}"


def check_consistent(snap):
    """Rules the stored data keeps whatever the operations were."""
    for label, spec in SCOPED.items():
        for row in snap[label].values():
            for name, target, _ in spec.fks:
                value = row[f"{name}_id"]
                if value is not None and not SCOPED[target].links:
                    owner = snap[target][value]["tenant_id"]
                    assert owner == row["tenant_id"], f"{label} {row['id']} crosses to {target}"
    for label, (_, left, right) in THROUGHS.items():
        for row in snap[label].values():
            owners = {
                snap[left][row[f"{left}_id"]]["tenant_id"],
                snap[right][row[f"{right}_id"]]["tenant_id"],
            }
            assert len(owners) == 1, f"{label} {row['id']} links two tenants"
    for label, (_, record) in SHARES.items():
        for row in snap[label].values():
            assert row["owner_id"] == snap[record][row["record_id"]]["tenant_id"]
            assert row["owner_id"] != row["grantee_id"]
    for pk in snap["article"]:
        owners = [
            row
            for row in snap["article_link"].values()
            if row["record_id"] == pk and row["access"] == "owner"
        ]
        assert len(owners) == 1, f"article {pk} has {len(owners)} owners"


# Running as a tenant


_pending = {}


def fuzz_view(request, **kwargs):
    action = _pending.pop("action")
    try:
        with transaction.atomic():
            _pending["result"] = (action(), None)
    except REFUSED as error:
        _pending["result"] = (None, error)
    return HttpResponse("ok")


urlpatterns = [path("t/<str:tenant_slug>/fuzz/", fuzz_view)]


def run_as(tenant, via, action):
    """Run action as tenant. Returns (result, refusal). A refused action is rolled back."""
    if via == "request":
        _pending["action"] = action
        try:
            Client().post(f"/t/{tenant.slug}/fuzz/")
        except REFUSED as error:
            # Raised as the request's transaction commits, so the view's result does not stand.
            _pending.pop("result", None)
            return None, error
        return _pending.pop("result")
    try:
        with tenant_context(tenant), transaction.atomic():
            return action(), None
    except REFUSED as error:
        return None, error


def held(label, pk):
    """A record as attacking code would hold it: loaded earlier, outside the tenant's view."""
    model = _model(label)
    instance = model._base_manager.using(BYPASS).get(pk=pk)
    instance._state.db = "default"
    return instance


def _wipe():
    order = [
        *THROUGHS,
        *SHARES,
        "article_link",
        "task",
        "file",
        "card",
        "photo",
        "sheet",
        "project",
        "folder",
        "album",
        "tag",
        "article",
        "tenant",
    ]
    bypass = connections[BYPASS]
    with bypass.cursor() as cursor:
        for label in order:
            table = bypass.ops.quote_name(_model(label)._meta.db_table)
            cursor.execute(f"DELETE FROM {table}")


def _seed():
    """The same records for each tenant, then a few shares and links between them."""
    tenants = [Tenant.objects.create(slug=slug, name=slug) for slug in SLUGS]
    made = []
    for tenant in tenants:
        with tenant_context(tenant):
            tag = Tag.objects.create(name="a")
            project = Project.objects.create(name="a")
            project.tags.add(tag)
            Task.objects.create(title="a", project=project, reviewer=project)
            folder = Folder.objects.create(name="a")
            folder.tags.add(tag)
            File.objects.create(title="a", folder=folder)
            Card.objects.create(title="b", folder=folder)
            album = Album.objects.create(name="a")
            Photo.objects.create(title="a", album=album)
            sheet = Sheet.objects.create(title="a", tag=tag)
            Article.objects.create(title="a")
        made.append((folder, album, sheet))
    acme, globex, _ = tenants
    with tenant_context(acme):
        share(made[0][0], globex, WRITE)
        share(made[0][1], globex, READ)
        share(made[0][2], globex, WRITE)
    with tenant_context(globex):
        share(made[1][0], acme, READ)
        # Globex's own records inside and pointing at Acme's shared folder.
        File.objects.create(title="b", folder=made[0][0])
        Card.objects.create(title="c", folder=made[0][0])
    return tenants


ACTOR = st.integers(0, len(SLUGS) - 1)
VIA = st.sampled_from(["context", "context", "request"])
PICK = st.integers(0, 10_000)
WORD = st.sampled_from(WORDS)
FILTER = st.builds(
    lambda kind, word, picks, tenant, rel: (kind, word, tuple(picks), tenant, rel),
    st.sampled_from(FILTERS),
    WORD,
    st.lists(PICK, max_size=3),
    ACTOR,
    st.integers(0, 5),
)


class TenantFuzz(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        _wipe()
        self.tenants = _seed()
        self.snap = snapshot()
        check_consistent(self.snap)

    # Helpers

    def pick(self, label, number, actor=None):
        """A row of any tenant. Given an actor, an even number picks one of its own rows."""
        keys = sorted(self.snap[label])
        if actor is not None and number % 2 == 0:
            me = self.tenants[actor].pk
            if label == "article":
                own = View(self.snap, me)
                keys = [pk for pk in keys if own.article_owner(pk) == me]
            else:
                keys = [pk for pk in keys if self.snap[label][pk]["tenant_id"] == me]
            number //= 2
        return keys[number % len(keys)] if keys else None

    def filter(self, label, raw):
        kind, word, picks, tenant, number = raw
        rels = RELATIONS[label]
        if kind in ("subquery", "exists"):
            rels = [rel for rel in rels if rel.kind != "m2m"]
        rel = rels[number % len(rels)] if rels else None
        if rel is None and kind in ("related", "subquery", "exists"):
            kind = "related"
            rel = RELATIONS[label][number % len(RELATIONS[label])]
        pks = tuple(self.pick(label, p) for p in picks)
        return Filter(kind, word, pks, self.tenants[tenant].pk, rel)

    def act(self, actor, via, name, action, read=False):
        """Run one step as a tenant, then check what it did to the stored data."""
        tenant = self.tenants[actor]
        before = self.snap
        result, error = run_as(tenant, via, action)
        outcome = f"{name}: {'ok' if error is None else type(error).__name__}"
        event(outcome)
        OUTCOMES[outcome] += 1
        after = snapshot()
        if read:
            assert error is None, f"{name} failed: {error!r}"
            assert after == before, f"{name} changed data"
        check_write(before, after, tenant.pk)
        check_consistent(after)
        self.snap = after
        return result, error, View(before, tenant.pk)

    # Reads

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from(list(SCOPED)),
        raw=FILTER,
        shape=st.sampled_from(SHAPES),
        word=WORD,
    )
    def read(self, actor, via, label, raw, shape, word):
        found = self.filter(label, raw)
        result, _, view = self.act(
            actor,
            via,
            f"read {found.kind} {shape}",
            lambda: run_shape(found.apply(label), shape, label, word),
            read=True,
        )
        ids = found.expected_ids(view, label, locked=shape == "locked")
        assert result == expect_shape(view, ids, shape, label, word), (label, found, shape)

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from(list(SCOPED)),
        number=st.integers(0, 5),
        kind=st.sampled_from(RELATED_READS),
        held_pick=st.none() | PICK,
    )
    def read_related(self, actor, via, label, number, kind, held_pick):
        rels = RELATIONS[label]
        if kind in ("select_related", "fk_access"):
            rels = [rel for rel in rels if rel.kind == "forward"]
        elif kind in ("annotate", "aggregate", "manager", "prefetch_filtered"):
            rels = [rel for rel in rels if rel.kind != "forward"]
        if not rels:
            return
        rel = rels[number % len(rels)]
        held_row = None
        instance = None
        if kind == "manager" and held_pick is not None:
            pk = self.pick(label, held_pick)
            if pk is None:
                return
            held_row = self.snap[label][pk]
            instance = held(label, pk)
        result, _, view = self.act(
            actor,
            via,
            f"read {kind} {rel.kind}",
            lambda: run_related(label, rel, kind, instance),
            read=True,
        )
        assert result == expect_related(view, label, rel, kind, held_row), (label, rel, kind)

    @rule(actor=ACTOR, via=VIA, label=st.sampled_from(list(SCOPED)))
    def read_owned(self, actor, via, label):
        model = SCOPED[label].model
        result, _, view = self.act(
            actor,
            via,
            "read owned",
            lambda: sorted(model.objects.owned().values_list("pk", flat=True)),
            read=True,
        )
        assert result == sorted(pk for pk, row in view.s[label].items() if view.owns(label, row))

    @rule(actor=ACTOR, via=VIA, label=st.sampled_from(list(SHARES)))
    def read_shares(self, actor, via, label):
        model = SHARES[label][0]
        result, _, view = self.act(
            actor,
            via,
            "read shares",
            lambda: sorted(model.objects.values_list("pk", flat=True)),
            read=True,
        )
        expected = [
            pk
            for pk, row in sorted(view.s[label].items())
            if view.t in (row["owner_id"], row["grantee_id"])
        ]
        assert result == expected

    @rule(actor=ACTOR, via=VIA, kind=st.sampled_from(["records", "writable", "manager", "links"]))
    def read_articles(self, actor, via, kind):
        if kind == "manager" and not POSTGRES:
            return  # A known gap: third-party managers are filtered by row-level security only.

        def action():
            if kind == "records":
                return sorted(external.external_records(Article).values_list("pk", flat=True))
            if kind == "writable":
                rows = external.external_records(Article, write_only=True)
                return sorted(rows.values_list("pk", flat=True))
            if kind == "manager":
                return sorted(Article.objects.values_list("pk", flat=True))
            return sorted(ArticleTenantLink.objects.values_list("pk", flat=True))

        result, _, view = self.act(actor, via, f"read articles {kind}", action, read=True)
        if kind == "links":
            expected = [
                pk
                for pk, row in sorted(view.s["article_link"].items())
                if row["tenant_id"] == view.t or view.article_owner(row["record_id"]) == view.t
            ]
        else:
            allowed = {"owner", WRITE} if kind == "writable" else {"owner", WRITE, READ}
            expected = [pk for pk in sorted(view.s["article"]) if view.article_access(pk) & allowed]
        assert result == expected, kind

    @rule(actor=ACTOR, via=VIA, label=st.sampled_from(LABELS[:-1]), manager=st.booleans())
    def read_raw(self, actor, via, label, manager):
        if not POSTGRES:
            return  # A known gap: raw SQL is filtered by row-level security only.
        model = _model(label)
        table = connection.ops.quote_name(model._meta.db_table)

        def action():
            if manager and label in SCOPED:
                return sorted(row.pk for row in model.objects.raw(f"SELECT * FROM {table}"))
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT id FROM {table} ORDER BY id")
                return [row[0] for row in cursor.fetchall()]

        result, _, view = self.act(actor, via, "read raw", action, read=True)
        rows = sorted(view.s[label].items())
        if label == "article_link":
            expected = [
                pk
                for pk, row in rows
                if row["tenant_id"] == view.t or view.article_owner(row["record_id"]) == view.t
            ]
        else:
            expected = [pk for pk, row in rows if view.joined(label, row)]
        assert result == expected, label

    # Writes

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from(list(SCOPED)),
        method=st.sampled_from(["create", "save", "bulk", "bulk_ignore", "save_forged"]),
        word=WORD,
        picks=st.lists(PICK, min_size=2, max_size=2),
        null_reviewer=st.booleans(),
        owner=st.none() | ACTOR,
        forged=PICK,
    )
    def create(self, actor, via, label, method, word, picks, null_reviewer, owner, forged):
        spec = SCOPED[label]
        values = {spec.text: word}
        for (name, target, nullable), number in zip(spec.fks, picks, strict=False):
            values[f"{name}_id"] = None if nullable and null_reviewer else self.pick(target, number)
        if None in [values[f"{name}_id"] for name, _, nullable in spec.fks if not nullable]:
            return
        if owner is not None:
            values["tenant_id"] = self.tenants[owner].pk
        forged_pk = self.pick(label, forged)
        model = spec.model

        def action():
            if method == "create":
                return model.objects.create(**values).pk
            if method == "save":
                row = model(**values)
                row.save()
                return row.pk
            if method == "bulk":
                return [row.pk for row in model.objects.bulk_create([model(**values)])]
            if method == "bulk_ignore":
                model.objects.bulk_create([model(pk=forged_pk, **values)], ignore_conflicts=True)
                return None
            row = model(pk=forged_pk, **values)
            row.save()
            return row.pk

        before = self.snap
        _, error, _ = self.act(actor, via, f"create {method}", action)
        me = self.tenants[actor].pk
        own_keys = all(
            values[f"{name}_id"] is None or before[target][values[f"{name}_id"]]["tenant_id"] == me
            for name, target, _ in spec.fks
        )
        if method in ("create", "save", "bulk") and owner in (None, actor) and own_keys:
            assert error is None, f"own create refused: {error!r}"
            assert len(self.snap[label]) == len(before[label]) + 1

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from(NO_KEYS),
        word=WORD,
        new=WORD,
        update=st.booleans(),
    )
    def get_or_create(self, actor, via, label, word, new, update):
        spec = SCOPED[label]
        model = spec.model

        def action():
            try:
                if update:
                    row, created = model.objects.update_or_create(
                        **{spec.text: word}, defaults={spec.text: new}
                    )
                else:
                    row, created = model.objects.get_or_create(**{spec.text: word})
            except MultipleObjectsReturned:
                return "multiple"
            return row.pk, created

        result, error, view = self.act(actor, via, "get_or_create", action)
        matches = [row for row in view.visible(label) if row[spec.text] == word]
        if update and any(not view.can(label, row, write=True) for row in matches):
            # Refused first, as the row lock would not see a record shared read-only.
            assert error is not None, "update_or_create() went past a read-only record"
        elif len(matches) > 1:
            assert result == "multiple"
        elif len(matches) == 1:
            assert result == (matches[0]["id"], False)
        else:
            assert error is None and result[1] is True

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from(list(SCOPED)),
        raw=FILTER,
        change=st.sampled_from(["text", "fk", "fk_object", "tenant"]),
        owned=st.booleans(),
        word=WORD,
        number=PICK,
    )
    def update(self, actor, via, label, raw, change, owned, word, number):
        spec = SCOPED[label]
        found = self.filter(label, raw)
        if change == "tenant":
            values = {"tenant_id": self.tenants[number % len(self.tenants)].pk}
        elif change.startswith("fk") and spec.fks:
            name, target, _ = spec.fks[number % len(spec.fks)]
            pk = self.pick(target, number)
            if pk is None:
                return
            values = {name: held(target, pk)} if change == "fk_object" else {f"{name}_id": pk}
        else:
            change = "text"
            values = {spec.text: word}

        def action():
            rows = found.apply(label)
            return (rows.owned() if owned else rows).update(**values)

        result, error, view = self.act(actor, via, f"update {change}", action)
        if change != "text":
            return
        matched = set(found.expected_ids(view, label))
        if owned:
            matched = {pk for pk in matched if view.owns(label, view.s[label][pk])}
        if all(view.can(label, view.s[label][pk], write=True) for pk in matched):
            assert error is None, f"update refused: {error!r}"
            assert result == len(matched)
            assert all(self.snap[label][pk][spec.text] == word for pk in matched)
        else:
            assert error is not None, "a read-only record was updated"

    @rule(
        actor=ACTOR, via=VIA, label=st.sampled_from(list(SCOPED)), raw=FILTER, owned=st.booleans()
    )
    def delete(self, actor, via, label, raw, owned):
        found = self.filter(label, raw)

        def action():
            rows = found.apply(label)
            return (rows.owned() if owned else rows).delete()

        before = self.snap
        result, error, view = self.act(actor, via, "queryset delete", action)
        matched = set(found.expected_ids(view, label))
        if owned:
            matched = {pk for pk in matched if view.owns(label, view.s[label][pk])}
        gone = before[label].keys() - self.snap[label].keys()
        if error is None:
            assert gone == matched, "the delete skipped or added rows"
            # The count must not include rows the tenant cannot see.
            removed = sum(len(before[name].keys() - self.snap[name].keys()) for name in LABELS)
            assert result[0] == removed, f"delete reported {result}, removed {removed}"

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from(list(SCOPED)),
        number=PICK,
        source=st.sampled_from(["scoped", "held"]),
        change=st.sampled_from(["text", "fk", "tenant", "delete"]),
        partial=st.booleans(),
        word=WORD,
        aim=PICK,
    )
    def instance(self, actor, via, label, number, source, change, partial, word, aim):
        spec = SCOPED[label]
        pk = self.pick(label, number, actor)
        if pk is None:
            return
        instance = held(label, pk)
        lookups = []
        if change == "fk" and spec.fks:
            name, target_label, _ = spec.fks[aim % len(spec.fks)]
            field, value = f"{name}_id", self.pick(target_label, aim)
        elif change == "tenant":
            field, value = "tenant_id", self.tenants[aim % len(self.tenants)].pk
        else:
            change = "delete" if change == "delete" else "text"
            field, value = spec.text, word

        def action():
            row = instance
            if source == "scoped":
                row = spec.model.objects.filter(pk=pk).first()
                lookups.append(row is not None)
                if row is None:
                    return "hidden"
            if change == "delete":
                return row.delete()
            setattr(row, field, value)
            row.save(update_fields=[field] if partial else None)
            return "saved"

        result, error, view = self.act(actor, via, f"instance {source} {change}", action)
        row = view.s[label][pk]
        if lookups:
            assert lookups[0] == view.can(label, row), "scoped get disagrees with visibility"
        if change == "text" and result != "hidden":
            if view.can(label, row, write=True):
                assert error is None, f"save refused: {error!r}"
                assert self.snap[label][pk][spec.text] == word
            else:
                assert error is not None, "a record without write access was saved"

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from(list(SCOPED)),
        picks=st.lists(PICK, min_size=1, max_size=3),
        source=st.sampled_from(["scoped", "held"]),
        change=st.sampled_from(["text", "fk"]),
        word=WORD,
        aim=PICK,
    )
    def bulk_update(self, actor, via, label, picks, source, change, word, aim):
        spec = SCOPED[label]
        pks = sorted({self.pick(label, number, actor) for number in picks} - {None})
        if change == "fk" and spec.fks:
            name, target_label, _ = spec.fks[aim % len(spec.fks)]
            field, value = f"{name}_id", self.pick(target_label, aim)
        else:
            field, value = spec.text, word
        instances = [held(label, pk) for pk in pks]

        def action():
            rows = instances
            if source == "scoped":
                rows = list(spec.model.objects.filter(pk__in=pks))
            for row in rows:
                setattr(row, field, value)
            return spec.model.objects.bulk_update(rows, [field])

        self.act(actor, via, f"bulk_update {source}", action)

    @rule(
        actor=ACTOR,
        via=VIA,
        side=st.sampled_from(
            [("project", "tags"), ("tag", "project_set"), ("folder", "tags"), ("tag", "folder_set")]
        ),
        action_name=st.sampled_from(["add", "remove", "set", "clear"]),
        number=PICK,
        picks=st.lists(PICK, max_size=2),
        source=st.sampled_from(["scoped", "held"]),
    )
    def many_to_many(self, actor, via, side, action_name, number, picks, source):
        label, accessor = side
        other = {"tags": "tag", "project_set": "project", "folder_set": "folder"}[accessor]
        pk = self.pick(label, number, actor)
        target_pks = {self.pick(other, p, actor) for p in picks}
        if pk is None or None in target_pks:
            return
        base = held(label, pk)
        targets = [held(other, target) for target in sorted(target_pks)]

        def action():
            row = base
            if source == "scoped":
                row = SCOPED[label].model.objects.filter(pk=pk).first()
                if row is None:
                    return "hidden"
            manager = getattr(row, accessor)
            if action_name == "clear":
                return manager.clear()
            if action_name == "set":
                return manager.set(targets)
            return getattr(manager, action_name)(*targets)

        result, error, view = self.act(actor, via, f"m2m {action_name}", action)
        me = view.t
        all_mine = view.s[label][pk]["tenant_id"] == me and all(
            view.s[other][t.pk]["tenant_id"] == me for t in targets
        )
        if all_mine and action_name in ("add", "set") and result != "hidden":
            assert error is None, f"m2m {action_name} of own records refused: {error!r}"

    @rule(
        actor=ACTOR,
        via=VIA,
        relation=st.sampled_from(
            [
                ("project", "tasks", "task"),
                ("folder", "files", "file"),
                ("folder", "cards", "card"),
                ("album", "photos", "photo"),
            ]
        ),
        set_all=st.booleans(),
        bulk=st.booleans(),
        number=PICK,
        picks=st.lists(PICK, min_size=1, max_size=2),
    )
    def reverse_add(self, actor, via, relation, set_all, bulk, number, picks):
        label, accessor, child = relation
        pk = self.pick(label, number, actor)
        child_pks = {self.pick(child, p, actor) for p in picks}
        if pk is None or None in child_pks:
            return
        base = held(label, pk)
        children = [held(child, child_pk) for child_pk in sorted(child_pks)]

        def action():
            manager = getattr(base, accessor)
            if set_all:
                return manager.set(children, bulk=bulk)
            return manager.add(*children, bulk=bulk)

        self.act(actor, via, f"reverse {'set' if set_all else 'add'}", action)

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from(list(SHARED_LABEL)),
        number=PICK,
        grantee=ACTOR,
        access=st.sampled_from([READ, WRITE]),
        end=st.booleans(),
    )
    def sharing(self, actor, via, label, number, grantee, access, end):
        pk = self.pick(label, number, actor)
        if pk is None:
            return
        record = held(label, pk)
        tenant = self.tenants[grantee]

        def action():
            if end:
                return unshare(record, tenant)
            return share(record, tenant, access)

        _, error, view = self.act(actor, via, f"{'unshare' if end else 'share'}", action)
        if view.owns(label, view.s[label][pk]) and tenant.pk != view.t:
            assert error is None, f"owner's share refused: {error!r}"

    @rule(
        actor=ACTOR,
        via=VIA,
        kind=st.sampled_from(
            [
                "create",
                "save",
                "delete",
                "update",
                "records_delete",
                "share",
                "unshare",
                "manager_update",
                "manager_delete",
            ]
        ),
        number=PICK,
        grantee=ACTOR,
        access=st.sampled_from([READ, WRITE]),
        word=WORD,
        write_only=st.booleans(),
    )
    def articles(self, actor, via, kind, number, grantee, access, word, write_only):
        if kind.startswith("manager") and not POSTGRES:
            return  # A known gap: third-party managers are filtered by row-level security only.
        pk = self.pick("article", number, actor)
        record = held("article", pk) if pk is not None else None
        if record is None and kind in ("save", "delete", "share", "unshare"):
            return
        tenant = self.tenants[grantee]

        def action():
            if kind == "create":
                return Article.objects.create(title=word).pk
            if kind == "save":
                record.title = word
                record.save()
                return None
            if kind == "delete":
                # A refused delete marks the transaction for rollback, as docs/third-party.md says.
                with transaction.atomic():
                    return record.delete()
            if kind == "share":
                return external.share(record, tenant, access)
            if kind == "unshare":
                return external.unshare(record, tenant)
            rows = external.external_records(Article, write_only=write_only)
            if kind.startswith("manager"):
                rows = Article.objects.all()
            rows = rows.filter(title__in=WORDS)
            if kind.endswith("delete"):
                with transaction.atomic():
                    return rows.delete()
            return rows.update(title=word)

        result, error, view = self.act(actor, via, f"article {kind}", action)
        if kind == "update":
            writable = [
                pk
                for pk, row in view.s["article"].items()
                if row["title"] in WORDS and view.article_access(pk) & {"owner", WRITE}
            ]
            assert error is None and result == len(writable)
        if kind == "create":
            assert error is None, f"article create refused: {error!r}"

    @rule(
        actor=ACTOR,
        via=VIA,
        kind=st.sampled_from(
            [
                "update",
                "delete",
                "move",
                "insert",
                "share_insert",
                "share_upgrade",
                "link_insert",
                "through_insert",
                "article_insert",
            ]
        ),
        label=st.sampled_from([*SCOPED, "article"]),
        numbers=st.lists(PICK, min_size=3, max_size=3),
        access=st.sampled_from([READ, WRITE, "owner"]),
    )
    def raw_sql(self, actor, via, kind, label, numbers, access):
        if not POSTGRES:
            return  # A known gap: raw SQL is filtered by row-level security only.
        quote = connection.ops.quote_name
        first, second, third = numbers
        tenant_pks = [t.pk for t in self.tenants]
        tenant_pick = tenant_pks[third % len(tenant_pks)]
        if kind in ("update", "delete", "move"):
            if kind == "move" and label == "article":
                return
            table = quote(_model(label)._meta.db_table)
            pk = self.pick(label, first)
            text = "title" if label == "article" else SCOPED[label].text
            sql, params = {
                "update": (f"UPDATE {table} SET {quote(text)} = 'raw' WHERE id = %s", [pk]),
                "delete": (f"DELETE FROM {table} WHERE id = %s", [pk]),
                "move": (f"UPDATE {table} SET tenant_id = %s WHERE id = %s", [tenant_pick, pk]),
            }[kind]
        elif kind == "insert":
            label = NO_KEYS[first % len(NO_KEYS)]
            table = quote(SCOPED[label].model._meta.db_table)
            text = quote(SCOPED[label].text)
            sql, params = (
                f"INSERT INTO {table} (tenant_id, {text}) VALUES (%s, 'raw')",
                [tenant_pick],
            )
        elif kind in ("share_insert", "share_upgrade"):
            share_label = list(SHARES)[first % len(SHARES)]
            model, record = SHARES[share_label]
            table = quote(model._meta.db_table)
            if kind == "share_upgrade":
                pk = self.pick(share_label, second)
                if pk is None:
                    return
                sql, params = f"UPDATE {table} SET access = 'write' WHERE id = %s", [pk]
            else:
                record_pk = self.pick(record, second)
                owner = self.snap[record][record_pk]["tenant_id"] if second % 2 else tenant_pick
                grantee = tenant_pks[(third + 1) % len(tenant_pks)]
                sql = (
                    f"INSERT INTO {table} (record_id, owner_id, grantee_id, access, created_at) "
                    "VALUES (%s, %s, %s, %s, now())"
                )
                params = [record_pk, owner, grantee, READ if access == READ else WRITE]
        elif kind == "link_insert":
            table = quote(ArticleTenantLink._meta.db_table)
            sql = (
                f"INSERT INTO {table} (record_id, tenant_id, access, created_at) "
                "VALUES (%s, %s, %s, now())"
            )
            params = [self.pick("article", first), tenant_pick, access]
        elif kind == "through_insert":
            through = list(THROUGHS)[first % len(THROUGHS)]
            model, left, right = THROUGHS[through]
            table = quote(model._meta.db_table)
            sql = f"INSERT INTO {table} ({left}_id, {right}_id) VALUES (%s, %s)"
            params = [self.pick(left, second), self.pick(right, third)]
        else:
            table = quote(Article._meta.db_table)
            sql, params = f"INSERT INTO {table} (title) VALUES ('raw')", []
        if None in params:
            return

        def action():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.rowcount

        self.act(actor, via, f"raw {kind}", action)

    @rule(
        actor=ACTOR,
        via=VIA,
        label=st.sampled_from([*SHARES, "article_link"]),
        kind=st.sampled_from(
            ["create", "update", "delete", "save", "instance_delete", "bulk_create", "bulk_update"]
        ),
        number=PICK,
        grantee=ACTOR,
        access=st.sampled_from([READ, WRITE, "owner"]),
    )
    def share_rows(self, actor, via, label, kind, number, grantee, access):
        """Writes straight to the share and link tables, around share() and unshare()."""
        model = _model(label)
        record_label = "article" if label == "article_link" else SHARES[label][1]
        grantee_pk = self.tenants[grantee].pk
        if label != "article_link" and access == "owner":
            access = WRITE
        pk = self.pick(label, number)
        row = held(label, pk) if pk is not None else None
        if row is None and kind not in ("create", "bulk_create"):
            return
        values = {"record_id": self.pick(record_label, number, actor), "access": access}
        values["tenant_id" if label == "article_link" else "grantee_id"] = grantee_pk
        if values["record_id"] is None:
            return

        def action():
            if kind == "create":
                return model.objects.create(**values).pk
            if kind == "bulk_create":
                return model.objects.bulk_create([model(**values)])
            if kind == "update":
                return model.objects.filter(pk=pk).update(access=access)
            if kind == "delete":
                return model.objects.filter(pk=pk).delete()
            if kind == "instance_delete":
                return row.delete()
            row.access = access
            if kind == "bulk_update":
                return model.objects.bulk_update([row], ["access"])
            if label != "article_link":
                row.grantee_id = grantee_pk
            row.save()
            return None

        self.act(actor, via, f"{label} {kind}", action)


def test_fuzz_tenant_isolation(monkeypatch):
    # Access checks have their own tests. This one is about what a tenant's queries reach.
    monkeypatch.setattr(TenantMiddleware, "_authorize", lambda self, request, tenant: Outcome())
    with override_settings(ROOT_URLCONF=__name__):
        run_state_machine_as_test(
            TenantFuzz,
            settings=settings(
                max_examples=EXAMPLES,
                stateful_step_count=STEPS,
                deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
            ),
        )
    for outcome, count in sorted(OUTCOMES.items()):
        print(f"{count:6} {outcome}")
