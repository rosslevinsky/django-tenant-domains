"""Generates and checks the row-level security policies for tenant-scoped tables."""

import hashlib
from dataclasses import dataclass, replace

from django.apps import apps
from django.db import connections, transaction

from ..external import OWNER, link_of
from ..sharing import READ, WRITE, cascade_links, config_of, has_sharing
from . import SETTING_NAME, bypass_alias, protected_aliases

POLICY_NAME = "tenant_domains_isolation"
ACCESS_POLICY_NAME = "tenant_domains_access"
# Tables with shared records need a different rule for each kind of statement.
SPLIT_POLICY_NAMES = {
    "SELECT": "tenant_domains_read",
    "INSERT": "tenant_domains_insert",
    "UPDATE": "tenant_domains_update",
    "DELETE": "tenant_domains_delete",
}
ALL_POLICY_NAMES = (POLICY_NAME, ACCESS_POLICY_NAME, *SPLIT_POLICY_NAMES.values())
KEEP_OWNER_FUNCTION = "tenant_domains_keep_owner"
KEEP_OWNER_TRIGGER = "tenant_domains_keep_owner"
# pg_trigger.tgtype for BEFORE UPDATE ... FOR EACH ROW: ROW (1) | BEFORE (2) | UPDATE (16).
KEEP_OWNER_TRIGGER_TYPE = 19
# A WITH CHECK cannot see the old row, so it cannot stop a grantee taking a record over.
KEEP_OWNER_BODY = """
DECLARE
    col text;
BEGIN
    IF (SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
        RETURN NEW;
    END IF;
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF to_jsonb(NEW) -> col IS DISTINCT FROM to_jsonb(OLD) -> col THEN
            RAISE EXCEPTION 'Only the bypass login can change this column: %', col
                USING ERRCODE = '42501';
        END IF;
    END LOOP;
    RETURN NEW;
END
"""
KEEP_OWNER_SQL = (
    f"CREATE OR REPLACE FUNCTION {KEEP_OWNER_FUNCTION}() RETURNS trigger AS $${KEEP_OWNER_BODY}$$ "
    "LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp"
)
# Each policy's comment holds a fingerprint of the SQL that made it, and one of the policy as
# Postgres stored it. The first goes stale when the code changes, the second when someone edits
# the policy, its roles or its kind by hand. Postgres rewrites a policy's text, so the second is
# taken from Postgres's own rendering of it, right after creating it.
COMMENT_PREFIX = "tenant_domains sql="
POLICY_STATE_SQL = (
    "encode(sha256(convert_to(concat_ws('|', pol.polpermissive, pol.polcmd, pol.polroles, "
    "coalesce(pg_get_expr(pol.polqual, pol.polrelid), ''), "
    "coalesce(pg_get_expr(pol.polwithcheck, pol.polrelid), '')), 'UTF8')), 'hex')"
)


@dataclass(frozen=True)
class TablePolicy:
    table: str  # For people to read.
    sql_name: str  # Ready for SQL. May include a schema.
    expression: str = ""  # One rule for every statement, or the four below.
    read: str = ""
    insert: str = ""
    update_using: str = ""
    update_check: str = ""
    delete: str = ""
    keep_columns: tuple = ()
    setup: tuple = ()  # Statements to run before the policies.
    requires: tuple = ()  # Other tables the policies mention, ready for SQL.


def _is_scoped(model) -> bool:
    return getattr(model, "_is_tenant_scoped", False)


def _visible_through(link, table, quote) -> str:
    """A row is visible when the row it points to is, since that row's policy applies too."""
    parent_table = quote(link.related_model._meta.db_table)
    return (
        f"EXISTS (SELECT 1 FROM {parent_table} "
        f"WHERE {parent_table}.{quote(link.target_field.column)} "
        f"= {quote(table)}.{quote(link.column)})"
    )


def _current(field, connection) -> str:
    cast = field.db_type(connection)
    return f"NULLIF(current_setting('{SETTING_NAME}', true), '')::{cast}"


def _grant_exists(link_sql, share_model, connection, write_only, extra="") -> str:
    quote = connection.ops.quote_name
    share = quote(share_model._meta.db_table)
    grantee = share_model._meta.get_field("grantee")
    record = quote(share_model._meta.get_field("record").column)
    condition = f"{share}.{record} = {link_sql} AND {share}.{quote(grantee.column)} = "
    condition += _current(grantee, connection)
    if write_only:
        condition += f" AND {share}.{quote('access')} = '{WRITE}'"
    return f"EXISTS (SELECT 1 FROM {share}{extra} WHERE {condition})"


def _granted(model, connection, write_only) -> list[str]:
    quote = connection.ops.quote_name
    table = quote(model._meta.db_table)
    terms = []
    config = config_of(model)
    if config is not None:
        key = f"{table}.{quote(model._meta.pk.column)}"
        terms.append(_grant_exists(key, config.share_model, connection, write_only))
    for link in cascade_links(model):
        parent = link.related_model
        share_model = config_of(parent).share_model
        parent_table = quote(parent._meta.db_table)
        parent_tenant = quote(parent._meta.get_field("tenant").column)
        tenant = quote(model._meta.get_field("tenant").column)
        share = quote(share_model._meta.db_table)
        record = quote(share_model._meta.get_field("record").column)
        # Only the parent owner's children inherit the share.
        join = (
            f" JOIN {parent_table} ON {parent_table}.{quote(link.target_field.column)} = "
            f"{share}.{record} AND {parent_table}.{parent_tenant} = {table}.{tenant}"
        )
        key = f"{table}.{quote(link.column)}"
        terms.append(_grant_exists(key, share_model, connection, write_only, join))
    return terms


def _shared_table_policy(model, connection) -> TablePolicy:
    quote = connection.ops.quote_name
    db_table = model._meta.db_table
    field = model._meta.get_field("tenant")
    owned = f"{quote(db_table)}.{quote(field.column)} = {_current(field, connection)}"
    readable = " OR ".join([owned, *_granted(model, connection, False)])
    writable = " OR ".join([owned, *_granted(model, connection, True)])
    # Dropping a table drops the policies that read it, which would leave only the allow policy.
    config = config_of(model)
    share_models = [config.share_model] if config is not None else []
    parents = [link.related_model for link in cascade_links(model)]
    share_models += [config_of(parent).share_model for parent in parents]
    requires = [quote(other._meta.db_table) for other in (*share_models, *parents)]
    return TablePolicy(
        db_table.replace('"', ""),
        quote(db_table),
        read=readable,
        insert=owned,
        update_using=writable,
        update_check=writable,
        delete=owned,
        keep_columns=(field.column, model._meta.pk.column),
        requires=tuple(dict.fromkeys(requires)),
    )


def _share_table_policy(model, connection) -> TablePolicy:
    quote = connection.ops.quote_name
    db_table = model._meta.db_table
    table = quote(db_table)
    owner_field = model._meta.get_field("owner")
    grantee = model._meta.get_field("grantee")
    record = model._meta.get_field("record")
    parent = record.related_model
    current = _current(owner_field, connection)
    owner = f"{table}.{quote(owner_field.column)} = {current}"
    parent_table = quote(parent._meta.db_table)
    parent_tenant = quote(parent._meta.get_field("tenant").column)
    # The owner column could be forged, so a write also checks the record itself. Reads do not
    # look at the record, which is what stops the record's policy and this one recursing.
    owns_record = (
        f"EXISTS (SELECT 1 FROM {parent_table} WHERE "
        f"{parent_table}.{quote(record.target_field.column)} = {table}.{quote(record.column)} "
        f"AND {parent_table}.{parent_tenant} = {current})"
    )
    write_check = f"{owner} AND {owns_record}"
    return TablePolicy(
        db_table.replace('"', ""),
        table,
        read=f"{owner} OR {table}.{quote(grantee.column)} = {current}",
        insert=write_check,
        update_using=owner,
        update_check=write_check,
        delete=owner,
    )


def _many_to_many_policy(model, connection, sides) -> TablePolicy:
    """Reading follows both sides. Writing needs both sides owned, as a share never covers links."""
    quote = connection.ops.quote_name
    db_table = model._meta.db_table
    table = quote(db_table)
    owned_sides = []
    for field in sides:
        key = f"{table}.{quote(field.column)}"
        owned_sides.append(
            _owned_by_current(field.related_model, field.target_field.column, key, connection)
        )
    owned = " AND ".join(owned_sides)
    readable = " AND ".join(_visible_through(field, db_table, quote) for field in sides)
    return TablePolicy(
        db_table.replace('"', ""),
        table,
        read=readable,
        insert=owned,
        update_using=owned,
        update_check=owned,
        delete=owned,
    )


def _external_table_policy(model, link, connection) -> TablePolicy:
    """A third-party row has no tenant column, so its links say which tenants can reach it."""
    quote = connection.ops.quote_name
    db_table = model._meta.db_table
    table = quote(db_table)
    links = quote(link._meta.db_table)
    tenant = link._meta.get_field("tenant")
    record = quote(link._meta.get_field("record").column)
    key = f"{table}.{quote(model._meta.pk.column)}"

    def linked(*access):
        allowed = ", ".join(f"'{level}'" for level in access)
        kind = f" AND {links}.{quote('access')} IN ({allowed})" if access else ""
        return (
            f"EXISTS (SELECT 1 FROM {links} WHERE {links}.{record} = {key} "
            f"AND {links}.{quote(tenant.column)} = {_current(tenant, connection)}{kind})"
        )

    return TablePolicy(
        db_table.replace('"', ""),
        table,
        read=linked(),
        insert=linked(OWNER),
        update_using=linked(OWNER, WRITE),
        update_check=linked(OWNER, WRITE),
        delete=linked(OWNER),
        requires=(links,),
    )


def _qualified(sql_name, connection) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = to_regclass(%s)",
            [sql_name],
        )
        row = cursor.fetchone()
    return row[0] if row else sql_name  # Not created yet, and then the policy refuses every row.


def _link_table_policy(link, connection) -> TablePolicy:
    """The functions read past row-level security: a policy cannot query its own table."""
    quote = connection.ops.quote_name
    db_table = link._meta.db_table
    table = quote(db_table)
    tenant = link._meta.get_field("tenant")
    record = link._meta.get_field("record")
    target = record.related_model
    target_table = quote(target._meta.db_table)
    key_type = target._meta.pk.rel_db_type(connection)
    digest = hashlib.sha256(db_table.encode()).hexdigest()[:16]
    owns = f"tenant_domains_owns_{digest}"
    exists = f"tenant_domains_exists_{digest}"
    grants = f"tenant_domains_grants_{digest}"
    # The functions fix their search_path, so they name their tables in full.
    full_table = _qualified(table, connection)
    full_target = _qualified(target_table, connection)
    path = "pg_catalog, pg_temp"
    start = "RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER"
    setup = (
        (
            f"CREATE OR REPLACE FUNCTION {owns}({key_type}) {start} SET search_path = {path} AS "
            f"$tenant_domains$ SELECT EXISTS (SELECT 1 FROM {full_table} "
            f"WHERE {quote(record.column)} = $1 AND {quote('access')} = '{OWNER}' "
            f"AND {quote(tenant.column)} = {_current(tenant, connection)}) $tenant_domains$"
        ),
        (
            f"CREATE OR REPLACE FUNCTION {exists}({key_type}) {start} SET search_path = {path} AS "
            f"$tenant_domains$ SELECT EXISTS (SELECT 1 FROM {full_target} WHERE "
            f"{quote(target._meta.pk.column)} = $1) $tenant_domains$"
        ),
        # Holds the owner's link until commit. Otherwise the owner could delete the record while a
        # grant waits uncommitted, and the grant would attach to a new record given the same key.
        (
            f"CREATE OR REPLACE FUNCTION {grants}({key_type}) RETURNS boolean LANGUAGE sql "
            f"VOLATILE SECURITY DEFINER SET search_path = {path} AS "
            f"$tenant_domains$ SELECT count(*) > 0 FROM (SELECT 1 FROM {full_table} "
            f"WHERE {quote(record.column)} = $1 AND {quote('access')} = '{OWNER}' "
            f"AND {quote(tenant.column)} = {_current(tenant, connection)} FOR KEY SHARE) AS owner "
            f"$tenant_domains$"
        ),
    )
    column = f"{table}.{quote(record.column)}"
    access = f"{table}.{quote('access')}"
    current = f"{table}.{quote(tenant.column)} = {_current(tenant, connection)}"
    owned = f"{owns}({column})"
    other_tenant = f"{table}.{quote(tenant.column)} <> {_current(tenant, connection)}"
    # A tenant can only claim a record that does not exist yet, which the foreign key then requires
    # to exist by commit. That is what stops a tenant linking itself to another tenant's record.
    claim = f"{current} AND {access} = '{OWNER}' AND NOT {exists}({column})"
    grant = f"{other_tenant} AND {access} IN ('{READ}', '{WRITE}') AND {grants}({column})"
    change = f"{access} IN ('{READ}', '{WRITE}') AND {owned}"
    return TablePolicy(
        db_table.replace('"', ""),
        table,
        read=f"{current} OR {owned}",
        insert=f"({claim}) OR ({grant})",
        update_using=change,
        update_check=change,
        delete=f"{owned} AND ({access} <> '{OWNER}' OR NOT {exists}({column}))",
        keep_columns=(record.column, tenant.column, link._meta.pk.column),
        setup=setup,
        requires=(target_table,),
    )


def _owned_by_current(model, column, key, connection) -> str:
    """Whether the row of model where column = key belongs to the current tenant.

    The tenant column may be on an ancestor, reached through each parent link in turn.
    """
    quote = connection.ops.quote_name
    model = model._meta.concrete_model  # A proxy has no parent link of its own.
    table = quote(model._meta.db_table)
    tenant = model._meta.get_field("tenant")
    if tenant.model is model:
        owned = f"{table}.{quote(tenant.column)} = {_current(tenant, connection)}"
    else:
        link = model._meta.get_ancestor_link(tenant.model)
        owned = _owned_by_current(
            link.related_model,
            link.target_field.column,
            f"{table}.{quote(link.column)}",
            connection,
        )
    return f"EXISTS (SELECT 1 FROM {table} WHERE {table}.{quote(column)} = {key} AND {owned})"


def _external_many_to_many_policy(model, connection, sides) -> TablePolicy:
    """Reading needs a link to the record. Writing needs the right to change it."""
    quote = connection.ops.quote_name
    db_table = model._meta.db_table
    table = quote(db_table)

    def rule(field, write):
        parent = field.related_model
        key = f"{table}.{quote(field.column)}"
        link = link_of(parent)
        if link is None:
            if not write:
                # That table's own policy decides, which includes any shares.
                return _visible_through(field, db_table, quote)
            return _owned_by_current(parent, field.target_field.column, key, connection)
        links = quote(link._meta.db_table)
        tenant = link._meta.get_field("tenant")
        record = quote(link._meta.get_field("record").column)
        kind = f" AND {links}.{quote('access')} IN ('{OWNER}', '{WRITE}')" if write else ""
        return (
            f"EXISTS (SELECT 1 FROM {links} WHERE {links}.{record} = {key} "
            f"AND {links}.{quote(tenant.column)} = {_current(tenant, connection)}{kind})"
        )

    readable = " AND ".join(rule(field, False) for field in sides)
    writable = " AND ".join(rule(field, True) for field in sides)
    return TablePolicy(
        db_table.replace('"', ""),
        table,
        read=readable,
        insert=writable,
        update_using=writable,
        update_check=writable,
        delete=writable,
        requires=tuple(
            quote(link_of(field.related_model)._meta.db_table)
            for field in sides
            if link_of(field.related_model) is not None
        ),
    )


def _policy_for(model, connection) -> TablePolicy | None:
    if getattr(model, "_tenant_is_link_table", False):
        return _link_table_policy(model, connection)
    if link_of(model) is not None:
        return _external_table_policy(model, link_of(model), connection)
    if model._meta.auto_created:
        relations = [
            field
            for field in model._meta.concrete_fields
            if field.is_relation
            and (_is_scoped(field.related_model) or link_of(field.related_model) is not None)
        ]
        if any(link_of(field.related_model) is not None for field in relations):
            return _external_many_to_many_policy(model, connection, relations)
    if getattr(model, "_tenant_is_share_table", False):
        return _share_table_policy(model, connection)
    if _is_scoped(model) and model._meta.get_field("tenant").model is model and has_sharing(model):
        return _shared_table_policy(model, connection)
    if model._meta.auto_created:
        sides = [
            field
            for field in model._meta.concrete_fields
            if field.is_relation and _is_scoped(field.related_model)
        ]
        if any(has_sharing(field.related_model) for field in sides):
            return _many_to_many_policy(model, connection, sides)
    expression = _expression(model, connection)
    if not expression:
        return None
    db_table = model._meta.db_table
    return TablePolicy(db_table.replace('"', ""), connection.ops.quote_name(db_table), expression)


def _expression(model, connection) -> str | None:
    quote = connection.ops.quote_name
    table = model._meta.db_table
    if _is_scoped(model):
        field = model._meta.get_field("tenant")
        if field.model is model:
            cast = field.db_type(connection)
            return (
                f"{quote(field.column)} = "
                f"NULLIF(current_setting('{SETTING_NAME}', true), '')::{cast}"
            )
        # Multi-table inheritance: the tenant column is on an ancestor's table.
        for parent, link in model._meta.parents.items():
            if link is not None and _is_scoped(parent):
                return _visible_through(link, table, quote)
        return None
    if model._meta.auto_created:
        # A many-to-many table has no tenant column, and its rows belong to both sides.
        sides = [
            _visible_through(field, table, quote)
            for field in model._meta.concrete_fields
            if field.is_relation and _is_scoped(field.related_model)
        ]
        return " AND ".join(sides) if sides else None
    return None


def table_policies(connection) -> list[TablePolicy]:
    policies = []
    for model in apps.get_models(include_auto_created=True):
        # Unmanaged tables count too: the application's login can reach them all the same.
        if model._meta.proxy:
            continue
        policy = _policy_for(model, connection)
        if policy is not None:
            policies.append(policy)
    return policies


def _existing_tables(connection) -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def _located(connection) -> list[tuple[TablePolicy, int]]:
    """Pair each policy with its table's id. Postgres resolves schemas and quotes for us."""
    located = []
    with connection.cursor() as cursor:
        for policy in table_policies(connection):
            cursor.execute("SELECT to_regclass(%s)::oid", [policy.sql_name])
            oid = cursor.fetchone()[0]
            if oid is None:
                continue
            for required in policy.requires:
                # A migrate of one app can leave the other table not created yet. Refuse every
                # row until it is, as the application's login can already reach this one.
                cursor.execute("SELECT to_regclass(%s)::oid", [required])
                if cursor.fetchone()[0] is None:
                    policy = replace(policy, expression="false", setup=(), keep_columns=())
                    break
            located.append((policy, oid))
    return located


def _split_policies(policy: TablePolicy) -> list[str]:
    table = policy.sql_name

    def start(statement):
        name = SPLIT_POLICY_NAMES[statement]
        return f"CREATE POLICY {name} ON {table} AS RESTRICTIVE FOR {statement} "

    return [
        start("SELECT") + f"USING ({policy.read})",
        start("INSERT") + f"WITH CHECK ({policy.insert})",
        start("UPDATE") + f"USING ({policy.update_using}) WITH CHECK ({policy.update_check})",
        start("DELETE") + f"USING ({policy.delete})",
    ]


def _table_sql(policy: TablePolicy) -> list[str]:
    table = policy.sql_name
    statements = [
        *policy.setup,
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        *[f"DROP POLICY IF EXISTS {name} ON {table}" for name in ALL_POLICY_NAMES],
        # Postgres ORs permissive policies, so a project's own policy could widen access.
        # A restrictive one is ANDed with the rest, but needs a permissive one to apply to.
        f"CREATE POLICY {ACCESS_POLICY_NAME} ON {table} USING (true)",
    ]
    if policy.expression:
        statements.append(
            f"CREATE POLICY {POLICY_NAME} ON {table} AS RESTRICTIVE "
            f"USING ({policy.expression}) WITH CHECK ({policy.expression})"
        )
    else:
        statements += _split_policies(policy)
    if policy.keep_columns:
        trigger = f"CREATE TRIGGER {KEEP_OWNER_TRIGGER} BEFORE UPDATE ON {table} FOR EACH ROW "
        statements.append(f"DROP TRIGGER IF EXISTS {KEEP_OWNER_TRIGGER} ON {table}")
        statements.append(
            trigger + f"EXECUTE FUNCTION {KEEP_OWNER_FUNCTION}({_trigger_args(policy)})"
        )
    return statements


def _trigger_args(policy: TablePolicy) -> str:
    return ", ".join("'" + column.replace("'", "''") + "'" for column in policy.keep_columns)


def _policy_names(policy: TablePolicy) -> set[str]:
    restrictive = {POLICY_NAME} if policy.expression else set(SPLIT_POLICY_NAMES.values())
    return {ACCESS_POLICY_NAME, *restrictive}


def _fingerprint(policy: TablePolicy) -> str:
    statements = _table_sql(policy) + ([KEEP_OWNER_SQL] if policy.keep_columns else [])
    return hashlib.sha256("\n".join(statements).encode()).hexdigest()[:32]


def _comment_sql(policy: TablePolicy, name: str) -> str:
    """Record the fingerprints. Only Postgres can render the stored policy, so it builds the text."""
    table = policy.sql_name.replace("'", "''")
    state = (
        f"(SELECT {POLICY_STATE_SQL} FROM pg_catalog.pg_policy pol "
        f"WHERE pol.polrelid = '{table}'::regclass AND pol.polname = '{name}')"
    )
    text = f"'{COMMENT_PREFIX}{_fingerprint(policy)} state=' || {state}"
    return (
        f"DO $tenant_domains$ BEGIN EXECUTE 'COMMENT ON POLICY {name} ON {table} IS ' "
        f"|| quote_literal({text}); END $tenant_domains$"
    )


def policy_sql(connection) -> list[str]:
    located = _located(connection)
    statements = []
    if any(policy.keep_columns for policy, _ in located):
        statements.append(KEEP_OWNER_SQL)
    for policy, _ in located:
        statements += _table_sql(policy)
        statements += [_comment_sql(policy, name) for name in sorted(_policy_names(policy))]
    return statements


def grant_sql(connection) -> list[str]:
    """Let the application's logins use the tables, which the bypass login owns."""
    statements = []
    for alias in protected_aliases():
        user = connections[alias].settings_dict.get("USER")
        if not user or user == connection.settings_dict.get("USER"):
            continue
        role = connection.ops.quote_name(user)
        schema = connection.ops.quote_name(_schema(connection))
        # Table by table, because ON ALL TABLES includes views, and a view reads with its owner's
        # rights: here the bypass login's, which would show every tenant.
        statements += [
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {connection.ops.quote_name(table)} TO {role}"
            for table in sorted(_existing_tables(connection))
        ]
        statements.append(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")
    return statements


def _schema(connection) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        return cursor.fetchone()[0]


def sync_policies(using: str | None = None) -> None:
    """Create or replace every policy. Run as the login that owns the tables."""
    alias = using or bypass_alias()
    connection = connections[alias]
    with transaction.atomic(using=alias), connection.cursor() as cursor:
        for statement in policy_sql(connection) + grant_sql(connection):
            cursor.execute(statement)


def missing_policies(using: str | None = None) -> list[str]:
    """Return the tenant-scoped tables whose row-level security is missing or out of date.

    That is: not enabled and forced, a policy missing or extra, a policy that differs from what
    the code generates now or from what was created, or a missing or disabled owner trigger.
    """
    alias = using or bypass_alias()
    connection = connections[alias]
    located = _located(connection)
    if not located:
        return []
    oids = [oid for _, oid in located]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT oid FROM pg_catalog.pg_class "
            "WHERE oid = ANY(%s) AND relrowsecurity AND relforcerowsecurity",
            [oids],
        )
        forced = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            f"SELECT pol.polrelid, pol.polname, obj_description(pol.oid, 'pg_policy'), "
            f"{POLICY_STATE_SQL} FROM pg_catalog.pg_policy pol "
            f"WHERE pol.polrelid = ANY(%s) AND pol.polname = ANY(%s)",
            [oids, list(ALL_POLICY_NAMES)],
        )
        found = {}
        for oid, name, comment, state in cursor.fetchall():
            found.setdefault(oid, {})[name] = (comment, state)
        function = f"{KEEP_OWNER_FUNCTION}()"
        cursor.execute(
            "SELECT t.tgrelid, t.tgargs FROM pg_catalog.pg_trigger t "
            "JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid "
            "WHERE t.tgrelid = ANY(%s) AND t.tgname = %s AND NOT t.tgisinternal "
            "AND t.tgenabled IN ('O', 'A') AND t.tgtype = %s "
            "AND p.oid = to_regprocedure(%s) AND p.prosrc = %s",
            [oids, KEEP_OWNER_TRIGGER, KEEP_OWNER_TRIGGER_TYPE, function, KEEP_OWNER_BODY],
        )
        triggers = {oid: bytes(args) for oid, args in cursor.fetchall()}
    missing = []
    for policy, oid in located:
        policies = found.get(oid, {})
        prefix = f"{COMMENT_PREFIX}{_fingerprint(policy)} state="
        current = set(policies) == _policy_names(policy) and all(
            comment == prefix + state for comment, state in policies.values()
        )
        if policy.keep_columns:
            args = b"".join(column.encode() + b"\0" for column in policy.keep_columns)
            current = current and triggers.get(oid) == args
        if oid not in forced or not current:
            missing.append(policy.table)
    return sorted(missing)


def sync_after_migrate(sender, using, **kwargs):
    """Keep every table protected by default, including ones later migrations add."""
    if protected_aliases() and using == bypass_alias():
        sync_policies(using)
