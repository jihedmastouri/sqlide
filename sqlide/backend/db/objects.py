"""Object descriptors: one read-only summary of any catalog object.

Every row of the sidebar tree — a connection, a database, a category
folder, a table, and every leaf under it: a column, an index, a
trigger, a function, an event — resolves to an `ObjectInfo` built
here, and the frontend renders that one shape (frontend/object_info.py)
instead of a hand-written screen per object type.

The descriptors are assembled from the plain `Connector` interface
(list_tables, list_columns, list_indexes, list_triggers, get_ddl, …),
so nothing in here is dialect-aware and every adapter gets the views
for free — an adapter that answers a list with `[]` renders an empty
detail table rather than an error.

A kind with no builder of its own falls through to `_generic`, which
reports whatever is known about the row plus the DDL the server will
give for that name. That is the contract the sidebar relies on: a node
never opens a blank screen, however new or odd its kind.

Everything here queries the catalog, so call `describe` from a worker
thread (frontend/util.run_async), never from the GTK main loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from sqlide.backend.db.base import Connector, ConnectorError

#: Category node name -> the object kind its rows hold. Mirrors the
#: sidebar's own categories.
CATEGORY_KINDS = {
    "tables": "table",
    "views": "view",
    "functions": "function",
    "indexes": "index",
    "triggers": "trigger",
    "events": "event",
}

#: Human name per kind, for the header line.
TYPE_LABELS = {
    "connection": "Connection",
    "database": "Database",
    "category": "Group",
    "table": "Table",
    "view": "View",
    "column": "Column",
    "function": "Function",
    "index": "Index",
    "trigger": "Trigger",
    "event": "Event",
}


@dataclass(frozen=True)
class ObjectRef:
    """What a row in a detail table opens when it is activated."""

    kind: str
    name: str
    table: str = ""  # owning table, for the kinds that need one
    category: str = ""  # category rows only


@dataclass(frozen=True)
class DetailTable:
    """One titled grid under the summary.

    `links` runs parallel to `rows`: an entry is the object that row
    opens, or None for a row that is informational only.
    """

    title: str
    columns: list[str]
    rows: list[tuple[str, ...]]
    links: list[ObjectRef | None] = field(default_factory=list)
    empty_note: str = "(none)"
    #: The PROPERTY_SECTIONS slug this table is, where it is one, so a
    #: deep link from the sidebar can find it again (CORE-05). Empty
    #: for the detail tables of a plain info view.
    slug: str = ""

    def link(self, index: int) -> ObjectRef | None:
        if 0 <= index < len(self.links):
            return self.links[index]
        return None


@dataclass(frozen=True)
class ObjectInfo:
    """Everything the info view shows about one object."""

    kind: str
    name: str
    type_label: str
    path: str = ""  # "connection ▸ database ▸ Indexes ▸ idx_users_email"
    summary: list[tuple[str, str]] = field(default_factory=list)
    tables: list[DetailTable] = field(default_factory=list)
    ddl: str = ""
    note: str = ""  # shown when the catalog had nothing specific to say


def describe(
    connector: Connector,
    kind: str,
    name: str,
    *,
    table: str = "",
    category: str = "",
    path: str = "",
    detail: str = "",
) -> ObjectInfo:
    """The descriptor for one sidebar node. Never raises for an
    unknown kind: it falls back to the generic summary."""
    builder = _BUILDERS.get(kind)
    if builder is None:
        return _generic(connector, kind, name, path=path, detail=detail)
    info = builder(
        connector, kind, name, table=table, category=category, path=path
    )
    return _replace_path(info, path)


def _replace_path(info: ObjectInfo, path: str) -> ObjectInfo:
    if not path or info.path:
        return info
    return ObjectInfo(
        kind=info.kind,
        name=info.name,
        type_label=info.type_label,
        path=path,
        summary=info.summary,
        tables=info.tables,
        ddl=info.ddl,
        note=info.note,
    )


def _label(kind: str) -> str:
    return TYPE_LABELS.get(kind, kind.replace("_", " ").capitalize() or "Object")


def _safe(call, default):
    """A catalog call that is allowed to be unsupported.

    Half the descriptors are assembled from optional lists; an adapter
    that cannot answer one (or a server that refuses) should cost that
    section, not the whole view.
    """
    try:
        return call()
    except ConnectorError:
        return default
    except Exception:  # a driver that escaped its wrapper
        return default


def _ddl(connector: Connector, name: str) -> str:
    return _safe(lambda: connector.get_ddl(name), "") or ""


# Builders, one per kind. All take the same signature so `describe`
# can dispatch on a dict.


def _connection(connector, kind, name, *, table, category, path):
    databases = _safe(connector.list_databases, [])
    schemas = _safe(connector.list_schemas, [])
    objects = _safe(connector.list_tables, []) if not databases else []
    summary = [
        ("Databases", str(len(databases)) if databases else "1"),
        ("Creatable kinds", ", ".join(_safe(connector.ddl_kinds, ())) or "—"),
        ("Accounts", "yes" if connector.supports_users else "no"),
        ("Drops objects", "yes" if connector.supports_drop else "no"),
    ]
    if schemas:
        summary.insert(1, ("Schemas", ", ".join(schemas)))
    tables = []
    if databases:
        tables.append(DetailTable(
            title="Databases",
            columns=["Name"],
            rows=[(db,) for db in databases],
            links=[ObjectRef("database", db) for db in databases],
        ))
    else:
        tables.append(_objects_table(objects))
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind),
        summary=summary, tables=tables,
    )


def _database(connector, kind, name, *, table, category, path):
    objects = _safe(connector.list_tables, [])
    views = [o for o in objects if o.kind == "view"]
    summary = [
        ("Tables", str(len(objects) - len(views))),
        ("Views", str(len(views))),
        ("Functions", str(len(_safe(connector.list_functions, [])))),
        ("Indexes", str(len(_safe(connector.list_indexes, [])))),
    ]
    schema = _safe(connector.current_schema, "")
    if schema:
        summary.append(("Current schema", schema))
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind),
        summary=summary, tables=[_objects_table(objects)],
    )


def _objects_table(objects) -> DetailTable:
    return DetailTable(
        title="Tables and views",
        columns=["Name", "Kind"],
        rows=[(o.name, o.kind) for o in objects],
        links=[ObjectRef(o.kind, o.name) for o in objects],
        empty_note="(no tables or views)",
    )


def _category(connector, kind, name, *, table, category, path):
    """A folder row: the list of what is inside it, most relevant
    columns first, every row opening its own info view."""
    slug = (category or name).lower()
    child = CATEGORY_KINDS.get(slug, "")
    if slug in ("tables", "views"):
        objects = [
            o for o in _safe(connector.list_tables, [])
            if (o.kind == "view") == (slug == "views")
        ]
        detail = DetailTable(
            title=name,
            columns=["Name", "Columns"],
            rows=[
                (o.name, str(len(_safe(
                    lambda o=o: connector.list_columns(o.name), []
                ))))
                for o in objects
            ],
            links=[ObjectRef(child, o.name) for o in objects],
        )
    elif slug == "functions":
        functions = _safe(connector.list_functions, [])
        detail = DetailTable(
            title=name,
            columns=["Name"],
            rows=[(f.name,) for f in functions],
            links=[ObjectRef("function", f.name) for f in functions],
        )
    elif slug == "indexes":
        indexes = _safe(connector.list_indexes, [])
        detail = DetailTable(
            title=name,
            columns=["Name", "Table", "Definition"],
            rows=[
                (i.name, i.table, i.ddl or "(no definition available)")
                for i in indexes
            ],
            links=[ObjectRef("index", i.name, i.table) for i in indexes],
        )
    elif slug == "triggers":
        triggers = _safe(connector.list_triggers, [])
        detail = DetailTable(
            title=name,
            columns=["Name", "Table"],
            rows=[(t.name, t.table) for t in triggers],
            links=[ObjectRef("trigger", t.name, t.table) for t in triggers],
        )
    elif slug == "events":
        events = _safe(connector.list_events, [])
        detail = DetailTable(
            title=name,
            columns=["Name"],
            rows=[(e,) for e in events],
            links=[ObjectRef("event", e) for e in events],
        )
    else:
        detail = DetailTable(title=name, columns=["Name"], rows=[], links=[])
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind),
        summary=[("Contains", child or "objects"),
                 ("Count", str(len(detail.rows)))],
        tables=[detail],
    )


def _table(connector, kind, name, *, table, category, path):
    columns = _safe(lambda: connector.list_columns(name), [])
    indexes = [
        i for i in _safe(connector.list_indexes, []) if i.table == name
    ]
    triggers = [
        t for t in _safe(connector.list_triggers, []) if t.table == name
    ]
    relations = [
        r for r in _safe(connector.list_relations, []) if r.table == name
    ]
    keys = [c.name for c in columns if c.is_pk]
    summary = [
        ("Columns", str(len(columns))),
        ("Primary key", ", ".join(keys) or "—"),
        ("Indexes", str(len(indexes))),
        ("Triggers", str(len(triggers))),
    ]
    tables = [DetailTable(
        title="Columns",
        columns=["Name", "Type", "Nullable", "Key"],
        rows=[
            (c.name, c.type, "yes" if c.nullable else "no",
             "PK" if c.is_pk else "")
            for c in columns
        ],
        links=[ObjectRef("column", c.name, name) for c in columns],
        empty_note="(no columns)",
    )]
    if indexes:
        tables.append(DetailTable(
            title="Indexes",
            columns=["Name", "Definition"],
            rows=[(i.name, i.ddl or "(no definition available)")
                  for i in indexes],
            links=[ObjectRef("index", i.name, i.table) for i in indexes],
        ))
    if triggers:
        tables.append(DetailTable(
            title="Triggers",
            columns=["Name"],
            rows=[(t.name,) for t in triggers],
            links=[ObjectRef("trigger", t.name, t.table) for t in triggers],
        ))
    if relations:
        tables.append(DetailTable(
            title="Foreign keys",
            columns=["Column", "References"],
            rows=[(r.column, f"{r.ref_table}.{r.ref_column}")
                  for r in relations],
            links=[ObjectRef("table", r.ref_table) for r in relations],
        ))
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind),
        summary=summary, tables=tables, ddl=_ddl(connector, name),
    )


def _column(connector, kind, name, *, table, category, path):
    owner = table
    column = None
    for candidate in _safe(lambda: connector.list_columns(owner), []):
        if candidate.name == name:
            column = candidate
            break
    if column is None:
        return _generic(
            connector, kind, name, path=path,
            detail=f"column of {owner}" if owner else "",
        )
    referenced = [
        r for r in _safe(connector.list_relations, [])
        if r.table == owner and r.column == name
    ]
    summary = [
        ("Table", owner),
        ("Type", column.type),
        ("Nullable", "yes" if column.nullable else "no"),
        ("Primary key", "yes" if column.is_pk else "no"),
    ]
    if referenced:
        summary.append((
            "References",
            ", ".join(f"{r.ref_table}.{r.ref_column}" for r in referenced),
        ))
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind), summary=summary,
    )


def _index(connector, kind, name, *, table, category, path):
    match = None
    for candidate in _safe(connector.list_indexes, []):
        if candidate.name == name and (not table or candidate.table == table):
            match = candidate
            break
    owner = (match.table if match else table) or ""
    summary = [("Table", owner or "—")]
    ddl = (match.ddl if match else "") or _ddl(connector, name)
    tables = []
    if owner:
        columns = _safe(lambda: connector.list_columns(owner), [])
        indexed = [c for c in columns if _mentions(ddl, c.name)]
        if indexed:
            summary.append(
                ("Columns", ", ".join(c.name for c in indexed))
            )
            tables.append(DetailTable(
                title="Indexed columns",
                columns=["Name", "Type"],
                rows=[(c.name, c.type) for c in indexed],
                links=[ObjectRef("column", c.name, owner) for c in indexed],
            ))
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind), summary=summary,
        tables=tables, ddl=ddl,
        note="" if match else "Not found in the index catalog.",
    )


def _mentions(ddl: str, column: str) -> bool:
    """Is `column` named in this CREATE INDEX text? Best-effort: the
    dialects render index columns differently, and a wrong guess only
    costs a row of the "Indexed columns" table."""
    if not ddl:
        return False
    body = ddl[ddl.find("(") :] if "(" in ddl else ddl
    return column.lower() in body.lower()


def _trigger(connector, kind, name, *, table, category, path):
    match = None
    for candidate in _safe(connector.list_triggers, []):
        if candidate.name == name and (not table or candidate.table == table):
            match = candidate
            break
    owner = (match.table if match else table) or ""
    ddl = (match.ddl if match else "") or _ddl(connector, name)
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind),
        summary=[("Table", owner or "—")] + _trigger_summary(ddl),
        ddl=ddl,
        note="" if match else "Not found in the trigger catalog.",
    )


#: Words a CREATE TRIGGER uses for its timing and its event, read back
#: out of the DDL so the summary says when it fires without every
#: adapter having to grow a trigger catalog of its own.
_TIMINGS = ("BEFORE", "AFTER", "INSTEAD OF")
_EVENTS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


def _trigger_summary(ddl: str) -> list[tuple[str, str]]:
    upper = ddl.upper()
    timing = next((t for t in _TIMINGS if t in upper), "")
    events = [e for e in _EVENTS if e in upper]
    summary = []
    if timing:
        summary.append(("Timing", timing))
    if events:
        summary.append(("Event", ", ".join(events)))
    return summary


def _function(connector, kind, name, *, table, category, path):
    ddl = _ddl(connector, name)
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind),
        summary=[("Name", name), ("Definition", "available" if ddl else "—")],
        ddl=ddl,
    )


def _event(connector, kind, name, *, table, category, path):
    ddl = _ddl(connector, name)
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind),
        summary=[("Name", name), ("Scheduled", "yes")],
        ddl=ddl,
    )


def _generic(
    connector: Connector, kind: str, name: str, *,
    path: str = "", detail: str = "",
) -> ObjectInfo:
    """The fallback: a kind nothing here knows about still opens.

    Whatever the row itself carried (its name, its kind, the detail the
    tree showed next to it) plus the DDL the server gives for that
    name, which is often the whole answer.
    """
    summary = [("Name", name), ("Kind", kind or "unknown")]
    if detail:
        summary.append(("Detail", detail))
    return ObjectInfo(
        kind=kind or "object", name=name, type_label=_label(kind), path=path,
        summary=summary, ddl=_ddl(connector, name) if name else "",
        note="No specific view for this object type yet — "
             "this is what the catalog reports.",
    )


_BUILDERS = {
    "connection": _connection,
    "database": _database,
    "category": _category,
    "table": _table,
    "view": _table,
    "column": _column,
    "index": _index,
    "trigger": _trigger,
    "function": _function,
    "event": _event,
}


# Table properties (CORE-04): the same descriptor shape, but assembled
# section by section from a list the metadata provider chose, so an
# engine without policies or partitions never sees those headings.

#: Section slug -> heading, in the order the properties view shows
#: them. `general` is the summary block, `ddl` the definition editor;
#: everything between is a detail table.
PROPERTY_SECTIONS = (
    ("general", "General"),
    ("columns", "Columns"),
    ("constraints", "Constraints"),
    ("foreign_keys", "Foreign keys"),
    ("references", "References"),
    ("indexes", "Indexes"),
    ("triggers", "Triggers"),
    ("partitions", "Partitions"),
    ("rules", "Rules"),
    ("policies", "Policies"),
    ("dependencies", "Dependencies"),
    ("functions", "Functions"),
    ("permissions", "Permissions"),
    ("ddl", "Definition"),
)

PROPERTY_SECTION_LABELS = dict(PROPERTY_SECTIONS)

#: Sections the plain connector cannot fill: they need the metadata
#: provider (who holds a grant is a question about accounts and role
#: membership, not about the table). `table_properties` leaves them out
#: and db/metadata.py appends them in display order (CORE-11).
PROVIDER_SECTIONS = ("permissions",)

#: Section slug -> the sidebar node kind its rows are, for the sections
#: whose members are objects the tree already knows how to open
#: (CORE-05). A section not listed here has no children of its own in
#: the tree: it is a leaf that deep-links into the Properties view.
SECTION_CHILD_KINDS = {
    "columns": "column",
    "indexes": "index",
    "triggers": "trigger",
}


def table_properties(
    connector: Connector,
    name: str,
    sections: tuple[str, ...] | list[str],
    *,
    kind: str = "table",
    path: str = "",
) -> ObjectInfo:
    """Everything about one table, for the table tab's Properties side.

    `sections` is what the engine supports (metadata.property_sections);
    a section not in it is left out entirely, while a section that is
    in it but currently empty renders its "(none)" note — "this engine
    has no policies" and "this table has no policies yet" are different
    answers and the view says which one it is.
    """
    wanted = [slug for slug, _label in PROPERTY_SECTIONS if slug in sections]
    columns = _safe(lambda: connector.list_columns(name), [])
    tables: list[DetailTable] = []
    summary: list[tuple[str, str]] = []
    ddl = ""
    for slug in wanted:
        if slug == "general":
            summary = _general_summary(connector, name, columns)
        elif slug == "ddl":
            ddl = _ddl(connector, name)
        elif slug in PROVIDER_SECTIONS:
            continue
        else:
            table = _property_table(connector, name, slug, columns)
            # Every section carries its slug, so a deep link from the
            # sidebar can scroll to the section it named (CORE-05).
            tables.append(replace(table, slug=slug))
    return ObjectInfo(
        kind=kind, name=name, type_label=_label(kind), path=path,
        summary=summary, tables=tables, ddl=ddl,
    )


def _general_summary(connector, name, columns) -> list[tuple[str, str]]:
    stats = _safe(lambda: connector.table_stats(name), None)
    keys = [c.name for c in columns if c.is_pk]
    summary = [("Columns", str(len(columns))),
               ("Primary key", ", ".join(keys) or "—")]
    for label, value in (
        ("Kind", getattr(stats, "kind", "")),
        ("Owner", getattr(stats, "owner", "")),
        ("Storage engine", getattr(stats, "engine", "")),
        ("Size", getattr(stats, "size", "")),
        ("Rows", getattr(stats, "rows", "")),
        ("Comment", getattr(stats, "comment", "")),
    ):
        if value:
            summary.append((label, str(value)))
    return summary


def _property_table(connector, name, slug, columns) -> DetailTable:
    title = PROPERTY_SECTION_LABELS.get(slug, slug.capitalize())
    if slug == "columns":
        return DetailTable(
            title=title,
            columns=["Name", "Type", "Nullable", "Key"],
            rows=[
                (c.name, c.type, "yes" if c.nullable else "no",
                 "PK" if c.is_pk else "")
                for c in columns
            ],
            links=[ObjectRef("column", c.name, name) for c in columns],
            empty_note="(no columns)",
        )
    if slug == "constraints":
        found = _safe(lambda: connector.list_constraints(name), [])
        return DetailTable(
            title=title,
            columns=["Name", "Kind", "Columns", "Definition"],
            rows=[(c.name, c.kind, c.columns, c.definition) for c in found],
            links=[None] * len(found),
            empty_note="(no constraints)",
        )
    if slug == "foreign_keys":
        found = [
            r for r in _safe(connector.list_relations, []) if r.table == name
        ]
        return DetailTable(
            title=title,
            columns=["Column", "References"],
            rows=[(r.column, f"{r.ref_table}.{r.ref_column}") for r in found],
            links=[ObjectRef("table", r.ref_table) for r in found],
            empty_note="(no foreign keys)",
        )
    if slug == "references":
        found = _safe(lambda: connector.list_references(name), [])
        return DetailTable(
            title=title,
            columns=["Table", "Column", "References"],
            rows=[(r.table, r.column, f"{name}.{r.ref_column}") for r in found],
            links=[ObjectRef("table", r.table) for r in found],
            empty_note="(nothing references this table)",
        )
    if slug == "indexes":
        found = [
            i for i in _safe(connector.list_indexes, []) if i.table == name
        ]
        return DetailTable(
            title=title,
            columns=["Name", "Definition"],
            rows=[(i.name, i.ddl or "(no definition available)")
                  for i in found],
            links=[ObjectRef("index", i.name, i.table) for i in found],
            empty_note="(no indexes)",
        )
    if slug == "triggers":
        found = [
            t for t in _safe(connector.list_triggers, []) if t.table == name
        ]
        return DetailTable(
            title=title,
            columns=["Name", "Definition"],
            rows=[(t.name, t.ddl or "(no definition available)")
                  for t in found],
            links=[ObjectRef("trigger", t.name, t.table) for t in found],
            empty_note="(no triggers)",
        )
    lister = {
        "partitions": "list_partitions",
        "rules": "list_rules",
        "policies": "list_policies",
        "dependencies": "list_dependencies",
        "functions": "list_table_functions",
    }[slug]
    found = _safe(lambda: getattr(connector, lister)(name), [])
    return DetailTable(
        title=title,
        columns=["Name", "Detail", "Definition"],
        rows=[(o.name, o.detail, o.definition) for o in found],
        links=[
            ObjectRef(o.kind, o.name) if o.kind else None for o in found
        ],
        empty_note=f"(no {title.lower()})",
    )
