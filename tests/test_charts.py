"""The chart model and its data mapping (CORE-30).

Every test here is pure: no connection, no GTK, no server. That is the
point of putting the spec in the backend — the series are a function of
(spec, result), so they can be asserted on directly.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from sqlide.backend import charts
from sqlide.backend.charts import (
    CATEGORICAL,
    NUMERIC,
    TEMPORAL,
    ChartSpec,
    classify,
    dump_state,
    from_dict,
    infer,
    load_state,
    series_from,
    to_dict,
    validate,
)


class Result:
    """The little of a ResultSet the mapping actually reads."""

    def __init__(self, columns, rows):
        self.columns = list(columns)
        self.rows = [tuple(r) for r in rows]


def points(data, name):
    for series in data.series:
        if series.name == name:
            return list(series.points)
    raise AssertionError(f"no series named {name!r}: {[s.name for s in data.series]}")


# The module's independence is an acceptance criterion, not a vibe.


def test_module_imports_no_gtk_and_no_driver():
    lines = open(charts.__file__).read().splitlines()
    imports = [
        line for line in lines if line.startswith(("import ", "from "))
    ]
    for line in imports:
        for banned in ("gi", "cairo", "sqlide.frontend", "sqlide.backend.db"):
            assert f" {banned}" not in line, line
    assert imports  # the check is worthless if nothing was found


# Classification


def test_classify_from_values():
    result = Result(
        ["day", "hits", "label"],
        [
            (date(2026, 1, 1), 3, "a"),
            (datetime(2026, 1, 2, 12), Decimal("4.5"), "b"),
        ],
    )
    classes = classify(result.columns, result.rows)
    assert classes == {"day": TEMPORAL, "hits": NUMERIC, "label": CATEGORICAL}


def test_classify_booleans_are_labels_not_measures():
    classes = classify(["flag"], [(True,), (False,)])
    assert classes["flag"] == CATEGORICAL


def test_classify_all_null_column_without_a_declared_type_is_categorical():
    classes = classify(["nothing"], [(None,), (None,)])
    assert classes["nothing"] == CATEGORICAL


def test_classify_prefers_the_declared_type_over_the_values():
    # All NULL to the values; a timestamp to the catalog, which knows.
    classes = classify(
        ["when", "n"],
        [(None, "12"), (None, "13")],
        provider={"when": "timestamp with time zone", "n": "bigint"},
    )
    assert classes == {"when": TEMPORAL, "n": NUMERIC}


def test_classify_takes_kinds_from_a_metadata_provider():
    class Provider:
        def column_kinds(self, table):
            assert table == "events"
            return {"n": "integer", "name": "text"}

    classes = classify(
        ["n", "name"], [(1, "a")], provider=Provider(), table="events"
    )
    assert classes == {"n": NUMERIC, "name": CATEGORICAL}


def test_classify_falls_back_to_values_when_the_provider_fails():
    class Angry:
        def column_kinds(self, table):
            raise RuntimeError("connection gone")

    classes = classify(["n"], [(1,), (2,)], provider=Angry())
    assert classes == {"n": NUMERIC}


def test_classify_mixed_column_is_a_label():
    classes = classify(["mixed"], [(1,), ("two",), (3,)])
    assert classes["mixed"] == CATEGORICAL


# Inference


def test_infer_time_series():
    result = Result(
        ["day", "signups"],
        [(date(2026, 1, 1), 5), (date(2026, 1, 2), 7)],
    )
    spec = infer(result).spec
    assert spec is not None
    assert (spec.type, spec.x, spec.series) == ("line", "day", ("signups",))


def test_infer_category_count_result_is_a_summed_bar():
    result = Result(["country", "n"], [("FR", 3), ("DE", 9)])
    spec = infer(result).spec
    assert spec is not None
    assert (spec.type, spec.x, spec.series) == ("bar", "country", ("n",))
    # A categorical X without aggregation draws one bar per row.
    assert spec.aggregation == "sum"


def test_infer_two_numeric_columns_is_a_scatter():
    result = Result(["width", "height"], [(1.0, 2.0), (3.0, 4.0)])
    spec = infer(result).spec
    assert spec is not None
    assert (spec.type, spec.x, spec.series) == ("scatter", "width", ("height",))
    assert spec.aggregation == "none"


def test_infer_uses_a_single_low_cardinality_category_as_the_split():
    result = Result(
        ["day", "plan", "revenue"],
        [
            (date(2026, 1, 1), "free", 1),
            (date(2026, 1, 1), "paid", 9),
            (date(2026, 1, 2), "free", 2),
        ],
    )
    spec = infer(result).spec
    assert spec is not None
    assert spec.x == "day" and spec.series == ("revenue",) and spec.split == "plan"


def test_infer_reports_a_reason_when_nothing_is_numeric():
    result = Result(["name", "city"], [("ana", "lyon"), ("bo", "berlin")])
    inference = infer(result)
    assert inference.spec is None
    assert not inference
    assert "numeric" in inference.reason.lower()


def test_infer_reports_a_reason_for_a_result_with_no_columns():
    inference = infer(Result([], []))
    assert inference.spec is None and inference.reason


# series_from: the mapping itself


def test_series_from_maps_a_time_series():
    result = Result(
        ["day", "signups"],
        [(date(2026, 1, 2), 7), (date(2026, 1, 1), 5)],
    )
    data = series_from(ChartSpec(type="line", x="day", series=("signups",)), result)
    assert data.x_kind == TEMPORAL
    # A temporal axis is sorted, whatever order the rows arrived in.
    assert points(data, "signups") == [
        (date(2026, 1, 1), 5.0),
        (date(2026, 1, 2), 7.0),
    ]
    assert (data.dropped, data.capped, data.rows) == (0, 0, 2)


@pytest.mark.parametrize(
    "how, expected",
    [
        ("sum", 10.0),
        ("count", 3.0),
        ("avg", 10.0 / 3),
        ("min", 2.0),
        ("max", 5.0),
    ],
)
def test_series_from_aggregates_duplicate_x_values(how, expected):
    result = Result(["k", "v"], [("a", 2), ("a", 3), ("a", 5)])
    data = series_from(
        ChartSpec(type="bar", x="k", series=("v",), aggregation=how), result
    )
    (_x, y), = points(data, "v")
    assert y == pytest.approx(expected)


def test_series_from_without_aggregation_keeps_every_row():
    result = Result(["k", "v"], [("a", 2), ("a", 3)])
    data = series_from(
        ChartSpec(type="scatter", x="k", series=("v",), aggregation="none"), result
    )
    assert points(data, "v") == [("a", 2.0), ("a", 3.0)]


def test_series_from_drops_nulls_and_non_numbers_and_counts_them():
    result = Result(
        ["k", "v"],
        [("a", 1), (None, 2), ("b", None), ("c", "not a number"), ("d", "4")],
    )
    data = series_from(ChartSpec(type="bar", x="k", series=("v",)), result)
    # The "4" is a number the driver handed back as text; the three
    # unusable rows are counted, not swallowed.
    assert data.dropped == 3
    assert points(data, "v") == [("a", 1.0), ("d", 4.0)]


def test_series_from_splits_series_by_a_column():
    result = Result(
        ["day", "plan", "n"],
        [
            (date(2026, 1, 1), "free", 1),
            (date(2026, 1, 1), "paid", 9),
            (date(2026, 1, 2), "free", 2),
        ],
    )
    data = series_from(
        ChartSpec(type="line", x="day", series=("n",), split="plan"), result
    )
    assert {s.name for s in data.series} == {"free", "paid"}
    assert points(data, "free") == [(date(2026, 1, 1), 1.0), (date(2026, 1, 2), 2.0)]


def test_series_from_caps_points_and_reports_the_cap():
    rows = [(float(i), float(i)) for i in range(10)]
    data = series_from(
        ChartSpec(type="line", x="x", series=("y",), point_cap=4),
        Result(["x", "y"], rows),
    )
    assert len(points(data, "y")) == 4
    assert data.capped == 6


def test_pie_folds_the_tail_into_one_other_slice():
    rows = [(f"c{i}", float(10 - i)) for i in range(6)]
    data = series_from(
        ChartSpec(type="pie", x="c", series=("n",), slice_cap=3),
        Result(["c", "n"], rows),
    )
    drawn = points(data, "n")
    assert len(drawn) == 3
    assert drawn[-1][0] == "Other"
    # The pie still adds up to the whole: 10+9+8+7+6+5 = 45.
    assert sum(y for _x, y in drawn) == pytest.approx(45.0)
    assert data.capped == 4
    assert data.x_labels[-1] == "Other"


def test_series_from_without_an_x_column_plots_against_the_row_number():
    data = series_from(
        ChartSpec(type="line", x="", series=("v",)), Result(["v"], [(3,), (4,)])
    )
    assert points(data, "v") == [(0.0, 3.0), (1.0, 4.0)]


def test_series_from_reports_a_missing_column_rather_than_raising():
    data = series_from(
        ChartSpec(type="line", x="gone", series=("v",)), Result(["v"], [(1,)])
    )
    assert not data
    assert data.series == ()
    assert "gone" in data.reason


def test_series_from_reports_an_unknown_type_rather_than_raising():
    data = series_from(
        ChartSpec(type="sunburst", x="k", series=("v",)), Result(["k", "v"], [("a", 1)])
    )
    assert not data and "sunburst" in data.reason


def test_series_from_reports_a_result_with_no_usable_row():
    data = series_from(
        ChartSpec(type="bar", x="k", series=("v",)),
        Result(["k", "v"], [("a", None), ("b", "x")]),
    )
    assert not data and data.reason
    assert data.dropped == 2


# Validation and serialisation


def test_validate_accepts_a_good_spec():
    spec = ChartSpec(type="bar", x="k", series=("v",))
    assert validate(spec, ["k", "v"]) == []


def test_validate_rejects_a_pie_with_several_value_columns():
    problems = validate(ChartSpec(type="pie", x="k", series=("a", "b")), ["k", "a", "b"])
    assert problems and "pie" in problems[0].lower()


def test_spec_round_trips_through_to_dict_and_from_dict():
    spec = ChartSpec(
        type="area",
        x="day",
        series=("a", "b"),
        split="plan",
        orientation="horizontal",
        aggregation="avg",
        stacked=True,
        slice_cap=5,
        point_cap=100,
        title="Revenue",
    )
    assert from_dict(to_dict(spec)) == spec


def test_from_dict_tolerates_unknown_keys_and_a_missing_shape():
    spec = from_dict({"type": "bar", "hyperdrive": True, "series": "v"})
    assert spec.type == "bar" and spec.series == ("v",)
    assert from_dict(None) == ChartSpec()


def test_from_dict_keeps_an_unknown_type_for_validate_to_report():
    spec = from_dict({"type": "sunburst", "series": ["v"]})
    assert spec.type == "sunburst"
    problems = validate(spec, ["v"])
    assert problems and "sunburst" in problems[0]


def test_state_round_trips_and_bad_state_is_no_state():
    spec = ChartSpec(type="pie", x="k", series=("v",), slice_cap=4)
    assert load_state(dump_state(spec)) == spec
    assert load_state("") is None
    assert load_state("{not json") is None
    assert load_state("[]") is None
    assert load_state('{"version": 99, "chart": {}}') is None
    assert load_state('{"version": 1}') is None


# Row provenance — the two-way selection contract (CORE-32)


def test_row_keys_name_the_mark_each_row_was_drawn_into():
    result = Result(
        ["city", "kind", "sales"],
        [("Paris", "web", 3), ("Paris", "shop", 4), ("Lyon", "web", 5)],
    )
    spec = ChartSpec(type="bar", x="city", series=("sales",), split="kind")
    assert charts.row_keys(spec, result) == [
        ("Paris", "web"),
        ("Paris", "shop"),
        ("Lyon", "web"),
    ]


def test_row_keys_agree_with_the_series_that_were_built():
    result = Result(
        ["city", "sales"],
        [("Paris", 3), ("Lyon", 5), ("Paris", 4)],
    )
    spec = ChartSpec(type="bar", x="city", series=("sales",), aggregation="sum")
    data = series_from(spec, result)
    keys = charts.row_keys(spec, result)
    drawn = {x for x, _y in data.series[0].points}
    assert {key[0] for key in keys if key} == drawn


def test_row_keys_drop_the_rows_the_chart_dropped():
    result = Result(
        ["city", "sales"], [("Paris", 3), (None, 4), ("Lyon", None)]
    )
    spec = ChartSpec(type="bar", x="city", series=("sales",))
    assert charts.row_keys(spec, result) == [("Paris", ""), None, None]


def test_row_keys_report_a_spec_the_result_no_longer_fits():
    result = Result(["city", "sales"], [("Paris", 3)])
    spec = ChartSpec(type="bar", x="region", series=("sales",))
    assert charts.row_keys(spec, result) == [None]
