"""Foreign-key navigation, decided over the catalog (CORE-43).

The relation data is already there: `Connector.list_relations()` and
`list_references()` hand back `RelationInfo` rows, one per key column.
This module turns those rows into the two moves a grid offers — "go to
the row this cell points at" and "show the rows that point at this
one" — as plain functions over plain data, so the decision is asserted
without a display and the frontend keeps no engine knowledge of its
own.

A `RelationInfo` names one column pair, so a composite key arrives as
several rows and two separate single-column keys into the same table
arrive as several rows too. Nothing in the row says which constraint
it came from, so the grouping here reads the referenced columns: a
composite key names each of them once, while two distinct keys into
the same table both land on it (`customers.id` twice). A repeat
therefore starts a new group, which is what tells `orders(a_id, b_id)
-> parts(id)` — two menu entries — from `orders(tenant, code) ->
parts(tenant, code)`, one entry with two conditions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from sqlide.backend.db.base import FilterCondition, RelationInfo


@dataclass(frozen=True)
class RelationTarget:
    """One navigation the grid can offer from the row under the cursor.

    `table` is the bare table name the tab opens, `schema` the schema
    it has to be opened in and is empty whenever the key stays inside
    the schema it was declared in — an engine without schemas never
    fills it. `columns` are the pairs the entry was built from, kept
    for the label and for tests to read.
    """

    table: str
    schema: str = ""
    incoming: bool = False
    filters: list[FilterCondition] = field(default_factory=list)
    columns: list[tuple[str, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        """The target as a menu entry should print it: qualified only
        where the key leaves its schema, the same rule the rest of the
        app writes names by."""
        return f"{self.schema}.{self.table}" if self.schema else self.table


def _same_table(rel_schema: str, schema: str) -> bool:
    """Whether a relation declared in `rel_schema` belongs to the table
    the tab is showing. An engine with no schema level fills neither
    side, and then every relation with the right name is ours."""
    return not rel_schema or not schema or rel_schema == schema


def outgoing(
    relations: Sequence[RelationInfo], table: str, schema: str = ""
) -> list[RelationInfo]:
    """The foreign keys `table` declares, out of the whole catalog."""
    return [
        r
        for r in relations
        if r.table == table and _same_table(r.schema, schema)
    ]


def incoming(
    references: Sequence[RelationInfo], table: str, schema: str = ""
) -> list[RelationInfo]:
    """The foreign keys pointing at `table`."""
    return [
        r
        for r in references
        if r.ref_table == table and _same_table(r.ref_schema, schema)
    ]


def foreign_key_columns(
    relations: Sequence[RelationInfo], table: str, schema: str = ""
) -> set[str]:
    """The columns of `table` that participate in a foreign key — what
    the header marks so the navigation is visible before the menu."""
    return {r.column for r in outgoing(relations, table, schema)}


def _grouped(
    rows: Sequence[RelationInfo], key, seen_of
) -> list[list[RelationInfo]]:
    """Split relation rows into one list per constraint.

    `key` says which other table a row is about, `seen_of` which
    column decides a repeat (see the module docstring): a column that
    has already been used by the group under construction means a
    second, separate key rather than another column of this one.
    """
    groups: dict[Any, list[list[RelationInfo]]] = {}
    for row in rows:
        buckets = groups.setdefault(key(row), [])
        for bucket in buckets:
            if seen_of(row) not in {seen_of(r) for r in bucket}:
                bucket.append(row)
                break
        else:
            buckets.append([row])
    return [bucket for buckets in groups.values() for bucket in buckets]


def outgoing_targets(
    relations: Sequence[RelationInfo],
    table: str,
    column: str,
    row: Mapping[str, Any],
    schema: str = "",
) -> list[RelationTarget]:
    """Where the cell in `column` of `row` points.

    One entry per foreign key `column` takes part in, each carrying a
    condition per column pair so a composite key opens the one row it
    names rather than every row sharing its first column. A key with a
    NULL anywhere in it points at nothing and is left out — an empty
    tab is not an answer.
    """
    mine = outgoing(relations, table, schema)
    groups = _grouped(
        mine,
        key=lambda r: (r.ref_schema, r.ref_table),
        seen_of=lambda r: r.ref_column,
    )
    targets = []
    for group in groups:
        if column not in {r.column for r in group}:
            continue
        if any(row.get(r.column) is None for r in group):
            continue
        first = group[0]
        targets.append(
            RelationTarget(
                table=first.ref_table,
                schema=first.ref_schema if first.cross_schema else "",
                filters=[
                    FilterCondition(r.ref_column, "=", str(row[r.column]))
                    for r in group
                ],
                columns=[(r.column, r.ref_column) for r in group],
            )
        )
    return targets


def incoming_targets(
    references: Sequence[RelationInfo],
    table: str,
    row: Mapping[str, Any],
    schema: str = "",
) -> list[RelationTarget]:
    """The tables whose rows point at this one, one entry per key.

    The filter runs the other way round: the referring columns equal
    this row's values for the columns they reference. A key whose
    referenced value is NULL matches nothing and is left out.
    """
    mine = incoming(references, table, schema)
    groups = _grouped(
        mine,
        key=lambda r: (r.schema, r.table),
        seen_of=lambda r: r.column,
    )
    targets = []
    for group in groups:
        if any(row.get(r.ref_column) is None for r in group):
            continue
        first = group[0]
        targets.append(
            RelationTarget(
                table=first.table,
                schema=first.schema if first.cross_schema else "",
                incoming=True,
                filters=[
                    FilterCondition(r.column, "=", str(row[r.ref_column]))
                    for r in group
                ],
                columns=[(r.ref_column, r.column) for r in group],
            )
        )
    return targets
