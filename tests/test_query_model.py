"""The query model and its renderer (CORE-17).

Every test here is pure: no connection, no GTK, no server. That is the
whole point of moving SQL generation out of the builder widget — the
statement is a function of the model, so it can be asserted on
directly.
"""

from __future__ import annotations

import pytest

from sqlide.backend.db import query_model
from sqlide.backend.db.base import ConnectorError
from sqlide.backend.db.query_model import (
    GENERIC,
    MYSQL,
    POSTGRES,
    SQLITE,
    AGGREGATES,
    Column,
    Condition,
    FilterGroup,
    Join,
    On,
    Order,
    Projection,
    QueryModel,
    TableRef,
    dialect_for,
    folded_group,
    from_dict,
    render,
    render_display,
    to_dict,
)


def sql(model, dialect=GENERIC) -> str:
    return render(model, dialect=dialect, formatted=False).sql


def users() -> QueryModel:
    return QueryModel(source=TableRef("users"))


# Projections


def test_no_projections_is_star():
    assert sql(users()).startswith('SELECT *\nFROM "users"')


def test_empty_model_renders_nothing():
    out = render(QueryModel())
    assert out.sql == ""
    assert out.params == []
    assert render_display(QueryModel()) == ""


def test_named_projections_are_quoted_in_order():
    model = QueryModel(
        source=TableRef("users"),
        projections=(
            Projection(column=Column("id")),
            Projection(column=Column("name")),
        ),
    )
    assert sql(model).startswith('SELECT "id", "name"\nFROM "users"')


def test_projection_alias_and_expression():
    model = QueryModel(
        source=TableRef("users"),
        projections=(Projection(expression="count(*)", alias="n"),),
    )
    assert sql(model).startswith('SELECT count(*) AS "n"')


def test_projection_with_neither_column_nor_expression_is_rejected():
    model = QueryModel(
        source=TableRef("users"), projections=(Projection(),)
    )
    with pytest.raises(ConnectorError):
        sql(model)


def test_distinct():
    model = QueryModel(source=TableRef("users"), distinct=True)
    assert sql(model).startswith("SELECT DISTINCT *")


# Sources


def test_schema_and_alias():
    model = QueryModel(source=TableRef("users", schema="app", alias="u"))
    assert '"app"."users" AS "u"' in sql(model)


def test_columns_stay_unqualified_with_one_source():
    model = QueryModel(
        source=TableRef("users"),
        projections=(Projection(column=Column("id", source="users")),),
    )
    assert 'SELECT "id"' in sql(model)


def test_columns_are_qualified_once_a_join_is_present():
    model = QueryModel(
        source=TableRef("users"),
        joins=(
            Join(
                "INNER JOIN",
                TableRef("orders"),
                on=(On(Column("id", "users"), Column("user_id", "orders")),),
            ),
        ),
        projections=(Projection(column=Column("id", source="users")),),
    )
    assert 'SELECT "users"."id"' in sql(model)


# Joins


@pytest.mark.parametrize(
    "kind", ["INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"]
)
def test_each_join_kind_renders_with_its_on_clause(kind):
    model = QueryModel(
        source=TableRef("users"),
        joins=(
            Join(
                kind,
                TableRef("orders"),
                on=(On(Column("id", "users"), Column("user_id", "orders")),),
            ),
        ),
    )
    assert (
        f'{kind} "orders" ON "users"."id" = "orders"."user_id"' in sql(model)
    )


def test_cross_join_takes_no_on_clause():
    model = QueryModel(
        source=TableRef("a"), joins=(Join("CROSS JOIN", TableRef("b")),)
    )
    assert 'CROSS JOIN "b"' in sql(model)
    assert "ON" not in sql(model)


def test_cross_join_with_an_on_clause_is_rejected():
    model = QueryModel(
        source=TableRef("a"),
        joins=(
            Join(
                "CROSS JOIN",
                TableRef("b"),
                on=(On(Column("x", "a"), Column("y", "b")),),
            ),
        ),
    )
    with pytest.raises(ConnectorError):
        sql(model)


def test_join_without_on_is_rejected():
    model = QueryModel(
        source=TableRef("a"), joins=(Join("LEFT JOIN", TableRef("b")),)
    )
    with pytest.raises(ConnectorError):
        sql(model)


def test_unknown_join_kind_is_rejected():
    model = QueryModel(
        source=TableRef("a"), joins=(Join("SIDEWAYS JOIN", TableRef("b")),)
    )
    with pytest.raises(ConnectorError):
        sql(model)


def test_composite_on_conditions_are_anded():
    model = QueryModel(
        source=TableRef("a"),
        joins=(
            Join(
                "INNER JOIN",
                TableRef("b"),
                on=(
                    On(Column("k1", "a"), Column("k1", "b")),
                    On(Column("k2", "a"), Column("k2", "b")),
                ),
            ),
        ),
    )
    assert '"a"."k1" = "b"."k1" AND "a"."k2" = "b"."k2"' in sql(model)


def test_self_join_by_alias():
    model = QueryModel(
        source=TableRef("emp", alias="e"),
        joins=(
            Join(
                "LEFT JOIN",
                TableRef("emp", alias="m"),
                on=(On(Column("mgr", "e"), Column("id", "m")),),
            ),
        ),
    )
    text = sql(model)
    assert '"emp" AS "e"' in text
    assert 'LEFT JOIN "emp" AS "m" ON "e"."mgr" = "m"."id"' in text


def test_a_dialect_without_a_join_kind_says_so():
    model = QueryModel(
        source=TableRef("a"),
        joins=(
            Join(
                "RIGHT JOIN",
                TableRef("b"),
                on=(On(Column("x", "a"), Column("y", "b")),),
            ),
        ),
    )
    # SQLite's floor is 3.25; RIGHT JOIN arrived in 3.39.
    with pytest.raises(ConnectorError):
        sql(model, SQLITE)
    assert "RIGHT JOIN" in sql(model, MYSQL)


# Filters


def test_condition_values_are_parameters_not_literals():
    model = QueryModel(
        source=TableRef("users"),
        where=FilterGroup(items=(Condition(Column("name"), "=", "o'brien"),)),
    )
    out = render(model, formatted=False)
    assert 'WHERE "name" = ?' in out.sql
    assert out.params == ["o'brien"]
    assert "o'brien" not in out.sql


def test_operators_without_a_value_bind_nothing():
    model = QueryModel(
        source=TableRef("users"),
        where=FilterGroup(items=(Condition(Column("name"), "IS NULL"),)),
    )
    out = render(model, formatted=False)
    assert 'WHERE "name" IS NULL' in out.sql
    assert out.params == []


def test_unknown_operator_is_rejected():
    model = QueryModel(
        source=TableRef("users"),
        where=FilterGroup(items=(Condition(Column("name"), "DROP"),)),
    )
    with pytest.raises(ConnectorError):
        sql(model)


def test_unknown_conjunction_is_rejected():
    model = QueryModel(
        source=TableRef("users"),
        where=FilterGroup(
            items=(
                Condition(Column("a"), "=", 1),
                Condition(Column("b"), "=", 2),
            ),
            conjunction="BUT",
        ),
    )
    with pytest.raises(ConnectorError):
        sql(model)


def test_nested_groups_parenthesise_and_keep_parameter_order():
    inner = FilterGroup(
        items=(
            Condition(Column("b"), "=", 2),
            Condition(Column("c"), "=", 3),
        ),
        conjunction="OR",
    )
    model = QueryModel(
        source=TableRef("t"),
        where=FilterGroup(
            items=(Condition(Column("a"), "=", 1), inner), conjunction="AND"
        ),
    )
    out = render(model, formatted=False)
    assert 'WHERE "a" = ? AND ("b" = ? OR "c" = ?)' in out.sql
    assert out.params == [1, 2, 3]


def test_or_of_two_ands_is_expressible():
    left = FilterGroup(
        items=(
            Condition(Column("a"), "=", 1),
            Condition(Column("b"), "=", 2),
        )
    )
    right = FilterGroup(items=(Condition(Column("c"), "=", 3),))
    model = QueryModel(
        source=TableRef("t"),
        where=FilterGroup(items=(left, right), conjunction="OR"),
    )
    assert 'WHERE ("a" = ? AND "b" = ?) OR "c" = ?' in sql(model)


def test_negated_group():
    model = QueryModel(
        source=TableRef("t"),
        where=FilterGroup(
            items=(Condition(Column("a"), "=", 1),), negated=True
        ),
    )
    assert 'WHERE NOT ("a" = ?)' in sql(model)


def test_empty_group_drops_the_where_clause():
    model = QueryModel(source=TableRef("t"), where=FilterGroup())
    assert "WHERE" not in sql(model)


def test_folded_group_reads_left_to_right():
    lines = [
        ("AND", Condition(Column("a"), "=", 1)),
        ("AND", Condition(Column("b"), "=", 2)),
        ("OR", Condition(Column("c"), "=", 3)),
    ]
    model = QueryModel(source=TableRef("t"), where=folded_group(lines))
    assert 'WHERE ("a" = ? AND "b" = ?) OR "c" = ?' in sql(model)
    assert folded_group([]) is None


# Grouping, ordering, limit, offset


def test_group_by_and_having():
    model = QueryModel(
        source=TableRef("t"),
        projections=(Projection(expression="count(*)"),),
        group_by=(Column("kind"),),
        having=FilterGroup(items=(Condition(Column("kind"), "!=", "x"),)),
    )
    out = render(model, formatted=False)
    assert 'GROUP BY "kind"' in out.sql
    assert 'HAVING "kind" != ?' in out.sql
    assert out.params == ["x"]


def test_order_by_directions_and_expressions():
    model = QueryModel(
        source=TableRef("t"),
        order_by=(
            Order(column=Column("a")),
            Order(column=Column("b"), descending=True),
            Order(expression="length(c)", descending=True),
        ),
    )
    assert 'ORDER BY "a" ASC, "b" DESC, length(c) DESC' in sql(model)


def test_order_entry_with_nothing_in_it_is_skipped():
    model = QueryModel(source=TableRef("t"), order_by=(Order(),))
    assert "ORDER BY" not in sql(model)


def test_limit_and_offset():
    model = QueryModel(source=TableRef("t"), limit=10, offset=20)
    text = sql(model)
    assert "LIMIT 10" in text
    assert "OFFSET 20" in text


def test_offset_without_limit_gets_a_stand_in_on_mysql():
    model = QueryModel(source=TableRef("t"), offset=20)
    assert "LIMIT" not in sql(model, POSTGRES)
    assert f"LIMIT {MYSQL.offset_limit_stand_in}" in sql(model, MYSQL)


def test_statement_ends_with_a_semicolon():
    assert sql(users()).endswith(";")


# Dialects


def test_quoting_per_dialect():
    model = QueryModel(
        source=TableRef("order"),
        projections=(Projection(column=Column("id")),),
    )
    assert 'SELECT "id"\nFROM "order"' in sql(model, POSTGRES)
    assert "SELECT `id`\nFROM `order`" in sql(model, MYSQL)
    assert 'SELECT "id"\nFROM "order"' in sql(model, SQLITE)


def test_quote_characters_inside_identifiers_are_doubled():
    model = QueryModel(source=TableRef('we"ird'))
    assert '"we""ird"' in sql(model, POSTGRES)
    model = QueryModel(source=TableRef("we`ird"))
    assert "`we``ird`" in sql(model, MYSQL)


def test_empty_and_nul_identifiers_are_rejected():
    with pytest.raises(ConnectorError):
        sql(QueryModel(source=TableRef("t"), group_by=(Column(""),)))
    with pytest.raises(ConnectorError):
        sql(QueryModel(source=TableRef("t\x00x")))


def test_placeholders_follow_the_driver():
    model = QueryModel(
        source=TableRef("t"),
        where=FilterGroup(items=(Condition(Column("a"), "=", 1),)),
    )
    assert 'WHERE "a" = %s' in sql(model, POSTGRES)
    assert "WHERE `a` = %s" in sql(model, MYSQL)
    assert 'WHERE "a" = ?' in sql(model, SQLITE)


def test_quote_argument_overrides_the_dialect():
    out = render(
        QueryModel(source=TableRef("t")),
        quote=lambda n: f"[{n}]",
        formatted=False,
    )
    assert "FROM [t]" in out.sql


class _FakeConnector:
    placeholder = "%s"

    def quote_ident(self, name: str) -> str:
        return "`" + name + "`"


class MysqlConnector(_FakeConnector):
    pass


class WeirdConnector(_FakeConnector):
    pass


def test_dialect_for_uses_the_connectors_own_quoting():
    dialect = dialect_for(MysqlConnector())
    assert dialect.name == "mysql"
    assert dialect.placeholder == "%s"
    assert "FULL JOIN" not in dialect.join_kinds
    assert dialect.quoted("t") == "`t`"


def test_dialect_for_an_unknown_engine_is_permissive():
    dialect = dialect_for(WeirdConnector())
    assert "FULL JOIN" in dialect.join_kinds
    assert dialect.quoted("t") == "`t`"


# Display form


def test_display_inlines_the_values_the_bound_form_binds():
    model = QueryModel(
        source=TableRef("users"),
        where=FilterGroup(
            items=(
                Condition(Column("name"), "=", "o'brien"),
                Condition(Column("age"), ">", 30),
            )
        ),
    )
    for dialect in (POSTGRES, MYSQL, SQLITE):
        shown = render_display(model, dialect=dialect, formatted=False)
        assert "'o''brien'" in shown
        assert "30" in shown
        assert "?" not in shown and "%s" not in shown


def test_formatted_output_keeps_the_driver_placeholder_intact():
    # The formatter lexes "%s" as two tokens; render() must put the
    # real marker in after formatting, not before.
    model = QueryModel(
        source=TableRef("t"),
        where=FilterGroup(items=(Condition(Column("a"), "=", 1),)),
    )
    out = render(model, dialect=POSTGRES)
    assert "%s" in out.sql
    assert "% s" not in out.sql


# Serialisation


def test_model_round_trips_through_plain_data():
    model = QueryModel(
        source=TableRef("users", schema="app", alias="u"),
        joins=(
            Join(
                "LEFT JOIN",
                TableRef("orders", alias="o"),
                on=(On(Column("id", "u"), Column("user_id", "o")),),
            ),
        ),
        projections=(
            Projection(column=Column("id", "u")),
            Projection(expression="count(*)", alias="n"),
        ),
        distinct=True,
        where=FilterGroup(
            items=(
                Condition(Column("name", "u"), "LIKE", "a%"),
                FilterGroup(
                    items=(Condition(Column("total", "o"), ">", 5),),
                    conjunction="OR",
                ),
            )
        ),
        group_by=(Column("id", "u"),),
        having=FilterGroup(items=(Condition(Column("total", "o"), ">", 1),)),
        order_by=(Order(column=Column("id", "u"), descending=True),),
        limit=50,
        offset=10,
    )
    import json

    restored = from_dict(json.loads(json.dumps(to_dict(model))))
    assert restored == model
    assert render(restored, dialect=POSTGRES) == render(
        model, dialect=POSTGRES
    )


def test_from_dict_tolerates_an_empty_or_partial_payload():
    assert from_dict({}) == QueryModel()
    assert from_dict({"source": {"name": "t"}}).source == TableRef("t")


# The persisted envelope (CORE-19)


def test_dump_state_round_trips_a_model() -> None:
    model = QueryModel(
        source=TableRef("orders"),
        joins=(
            Join(
                kind="LEFT JOIN",
                source=TableRef("users"),
                on=(On(Column("user_id", "orders"), Column("id", "users")),),
            ),
        ),
        projections=(Projection(column=Column("id", "orders")),),
        distinct=True,
        where=FilterGroup(items=(Condition(Column("name", "users"), "=", "ada"),)),
        order_by=(Order(column=Column("id", "orders"), descending=True),),
        limit=10,
    )
    assert query_model.load_state(query_model.dump_state(model)) == model


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"model": {}}',  # no version
        '{"version": 999, "model": {}}',  # written by a later build
        '{"version": 1}',  # no payload
        '{"version": 1, "model": "nonsense"}',
    ],
)
def test_load_state_never_raises_on_junk(text) -> None:
    assert query_model.load_state(text) is None


def test_unfold_group_inverts_folded_group() -> None:
    lines = [
        ("AND", Condition(Column("a"), "=", 1)),
        ("OR", Condition(Column("b"), "=", 2)),
        ("AND", Condition(Column("c"), "=", 3)),
    ]
    assert query_model.unfold_group(query_model.folded_group(lines)) == lines


def test_unfold_group_declines_a_tree_it_cannot_flatten() -> None:
    nested = FilterGroup(
        items=(
            FilterGroup(items=(Condition(Column("a"), "=", 1),)),
            FilterGroup(items=(Condition(Column("b"), "=", 2),)),
        )
    )
    assert query_model.unfold_group(nested) is None


# Aggregates, grouping and HAVING (CORE-21)


def _counted() -> QueryModel:
    """`how many orders per status`, the query a builder exists for."""
    return QueryModel(
        source=TableRef("orders"),
        projections=(
            Projection(column=Column("status")),
            Projection(column=Column("id"), function="COUNT", alias="orders"),
        ),
        group_by=(Column("status"),),
    )


def test_an_aliased_aggregate_renders_with_its_grouping():
    sql = render(_counted(), dialect=POSTGRES, formatted=False).sql
    assert sql == (
        'SELECT "status", COUNT("id") AS "orders"\n'
        'FROM "orders"\n'
        'GROUP BY "status";'
    )


def test_count_without_a_column_is_count_star():
    model = QueryModel(
        source=TableRef("t"),
        projections=(Projection(function="COUNT", alias="n"),),
    )
    assert 'COUNT(*) AS "n"' in render(
        model, dialect=POSTGRES, formatted=False
    ).sql


def test_count_distinct_is_the_aggregates_own_distinct():
    model = QueryModel(
        source=TableRef("orders"),
        projections=(
            Projection(column=Column("user_id"), function="COUNT", distinct=True),
        ),
    )
    sql = render(model, dialect=POSTGRES, formatted=False).sql
    assert 'COUNT(DISTINCT "user_id")' in sql
    # Not the statement-level DISTINCT, which nobody asked for.
    assert "SELECT DISTINCT" not in sql


def test_an_aggregate_other_than_count_needs_a_column():
    model = QueryModel(
        source=TableRef("t"), projections=(Projection(function="SUM"),)
    )
    with pytest.raises(ConnectorError):
        render(model)


def test_an_unknown_aggregate_is_refused():
    model = QueryModel(
        source=TableRef("t"),
        projections=(Projection(column=Column("a"), function="MEDIAN"),),
    )
    with pytest.raises(ConnectorError):
        render(model)


def test_an_aggregate_the_dialect_lacks_is_refused():
    from dataclasses import replace as _replace

    dialect = _replace(GENERIC, name="tiny", aggregates=("COUNT",))
    model = QueryModel(
        source=TableRef("t"),
        projections=(Projection(column=Column("a"), function="SUM"),),
    )
    with pytest.raises(ConnectorError):
        render(model, dialect=dialect)
    assert set(GENERIC.aggregates) == set(AGGREGATES)


def test_clauses_render_in_the_order_the_engine_reads_them():
    model = QueryModel(
        source=TableRef("orders"),
        projections=(
            Projection(column=Column("status")),
            Projection(column=Column("id"), function="COUNT", alias="n"),
        ),
        where=FilterGroup(items=(Condition(Column("total"), ">", 10),)),
        group_by=(Column("status"),),
        having=FilterGroup(
            items=(Condition(column=Column("id"), function="COUNT", op=">", value=2),)
        ),
        order_by=(Order(alias="n", descending=True),),
        limit=5,
    )
    out = render(model, dialect=POSTGRES, formatted=False)
    clauses = [
        line.split(" ")[0] for line in out.sql.splitlines()
    ]
    assert clauses == [
        "SELECT",
        "FROM",
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "LIMIT",
    ]
    # Both values are bound, in the order they are read.
    assert out.params == [10, 2]
    assert 'HAVING COUNT("id") > %s' in out.sql


def test_ordering_by_an_alias_falls_back_to_the_expression():
    from dataclasses import replace as _replace

    model = QueryModel(
        source=TableRef("orders"),
        projections=(
            Projection(column=Column("id"), function="COUNT", alias="n"),
        ),
        order_by=(Order(alias="n", descending=True),),
    )
    assert 'ORDER BY "n" DESC' in render(
        model, dialect=POSTGRES, formatted=False
    ).sql
    plain = _replace(POSTGRES, order_by_alias=False)
    assert 'ORDER BY COUNT("id") DESC' in render(
        model, dialect=plain, formatted=False
    ).sql


def test_a_computed_expression_is_passed_through_unchanged():
    model = QueryModel(
        source=TableRef("t"),
        projections=(Projection(expression="a * b", alias="area"),),
        order_by=(Order(expression="a * b"),),
    )
    sql = render(model, dialect=POSTGRES, formatted=False).sql
    assert 'a * b AS "area"' in sql
    assert "ORDER BY a * b ASC" in sql


def test_aggregates_round_trip_through_the_saved_state():
    model = QueryModel(
        source=TableRef("orders"),
        projections=(
            Projection(column=Column("status")),
            Projection(
                column=Column("user_id"),
                function="COUNT",
                distinct=True,
                alias="people",
            ),
            Projection(expression="total * 2", alias="doubled"),
        ),
        group_by=(Column("status"),),
        having=FilterGroup(
            items=(
                Condition(
                    column=Column("user_id"),
                    function="COUNT",
                    distinct=True,
                    op=">=",
                    value=2,
                ),
            )
        ),
        order_by=(Order(alias="people", descending=True),),
    )
    assert query_model.load_state(query_model.dump_state(model)) == model


def test_a_workspace_saved_before_aggregates_still_loads():
    # No function/distinct/alias keys anywhere: exactly what a
    # pre-CORE-21 build wrote.
    old = {
        "source": {"name": "orders"},
        "projections": [{"column": {"name": "id"}, "expression": "", "alias": ""}],
        "where": {
            "kind": "group",
            "conjunction": "AND",
            "items": [
                {
                    "kind": "condition",
                    "column": {"name": "id"},
                    "op": ">",
                    "value": 1,
                }
            ],
        },
        "order_by": [{"column": {"name": "id"}, "descending": True}],
    }
    model = query_model.from_dict(old)
    assert model.projections[0] == Projection(column=Column("id"))
    assert render(model, dialect=SQLITE, formatted=False).sql == (
        'SELECT "id"\nFROM "orders"\nWHERE "id" > ?\nORDER BY "id" DESC;'
    )
