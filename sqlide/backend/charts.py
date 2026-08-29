"""A serialisable chart definition and the mapping that fills it.

Nothing in the app turned a result set into something plottable: the
only chart objects that existed were `metrics.Chart`, which describe
server counters for the monitoring dashboard and say nothing about
columns, rows or axes. Those stay exactly where they are — this is not
a generalisation of them (see docs/charting-research.md).

This module is the third sibling of `db/query_model.py` (CORE-17) and
`db/table_model.py` (CORE-23), and follows the same three rules:

- **No frontend, no driver.** Nothing here imports GTK, cairo or a
  database module, so the whole mapping is testable without a server
  and the renderer (CORE-31) can be swapped without touching it.
- **No per-engine branch.** Column classification prefers the type the
  `MetadataProvider` declares and falls back to the Python values; a
  caller never asks "which engine is this".
- **Report, never raise.** A spec naming a column the result no longer
  has, or a chart type from a newer version, comes back as a `ChartData`
  carrying a `reason` — "chart not restored: column X is gone" — never
  as an exception or a blank canvas.

The pieces:

- `ChartSpec` — type, x column, series columns, split, orientation,
  client aggregation and per-type options, plus a `version` so CORE-33
  can migrate or discard cleanly.
- `classify(columns, rows, provider=)` — each column labelled
  `temporal`, `numeric` or `categorical`.
- `infer(result, classes=)` — the default mapping, or a reason why
  there is none.
- `series_from(spec, result)` — named series of (x, y) pairs, the axis
  kinds, and the counts it dropped or capped. It never truncates
  silently.
- `to_dict`/`from_dict`, `dump_state`/`load_state` — the persisted
  form CORE-33 writes into `TabState` and onto a saved query.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from sqlide.i18n import _, N_

__all__ = [
    "AGGREGATIONS",
    "CATEGORICAL",
    "CHART_TYPES",
    "ChartData",
    "ChartSpec",
    "DEFAULT_POINT_CAP",
    "DEFAULT_SLICE_CAP",
    "Inference",
    "MODEL_VERSION",
    "NUMERIC",
    "ORIENTATIONS",
    "OTHER_LABEL",
    "Series",
    "TEMPORAL",
    "classify",
    "dump_state",
    "from_dict",
    "infer",
    "load_state",
    "series_from",
    "to_dict",
    "validate",
]


#: The chart types the model can express — the five that cover
#: essentially every chart drawn from a SQL result (RS-03). Horizontal
#: bars are an orientation, not a sixth type.
CHART_TYPES = ("line", "bar", "area", "scatter", "pie")

#: Client-side aggregation over duplicate X values, and nothing more
#: than that: anything richer belongs in the query, where the builder's
#: aggregates (CORE-21) live.
AGGREGATIONS = ("none", "sum", "count", "avg", "min", "max")

#: Bar orientation. Applies to `bar` alone; the others ignore it.
ORIENTATIONS = ("vertical", "horizontal")

#: The three column classes. Everything a result can hold is one of
#: them: an ordered date/time axis, a measurable number, or a label.
TEMPORAL = "temporal"
NUMERIC = "numeric"
CATEGORICAL = "categorical"

#: A pie with 200 slices is a bug report waiting to happen, so the tail
#: is folded into one slice rather than drawn or dropped.
DEFAULT_SLICE_CAP = 12
#: Marked, not translated, at import time: the catalogue is not bound
#: yet here, so the lookup happens where the slice is built.
OTHER_LABEL = N_("Other")

#: Past a couple of thousand points the pixels stop distinguishing
#: them. The model caps and *says* it capped; decimation for drawing is
#: the renderer's business (CORE-31).
DEFAULT_POINT_CAP = 2000

#: Bumped whenever `to_dict`'s shape changes incompatibly. A spec from
#: a newer version is discarded rather than guessed at.
MODEL_VERSION = 1

#: Declared-type substrings that mean a date or a time. Matched against
#: the type text the catalog reports, lower-cased — deliberately loose,
#: because "timestamp with time zone", "DATETIME(6)" and "TIMESTAMPTZ"
#: are all the same axis.
_TEMPORAL_HINTS = ("date", "time", "year")
#: ...and the ones that mean a measurable number. `column_kinds` (the
#: MetadataProvider's own vocabulary) says "integer"/"number" and both
#: land here.
_NUMERIC_HINTS = (
    "int",
    "serial",
    "number",
    "numeric",
    "decimal",
    "dec",
    "float",
    "double",
    "real",
    "money",
    "fixed",
)
#: A type that is a number to the driver but a label to a chart:
#: summing account IDs is never what anybody meant. Only ever a hint —
#: an explicit spec still charts them if the user asks.
_NOT_MEASURE_HINTS = ("bool", "bit")

#: How many distinct values a categorical column may have and still be
#: worth splitting series by. More than this and the legend is longer
#: than the chart.
_SPLIT_CARDINALITY = 12


# Model


@dataclass(frozen=True)
class ChartSpec:
    """One chart, as a mapping of result columns onto axes.

    Columns are named, never indexed: re-running the query and
    redrawing is then the whole refresh story, and a result whose
    shape changed is *reported* rather than mis-plotted.
    """

    type: str = "line"
    #: The column on the X axis. Empty means "the row number", which
    #: is what a result with nothing ordered to plot against gets.
    x: str = ""
    #: The numeric columns drawn as lines/bars/slices, in order.
    series: tuple[str, ...] = ()
    #: A low-cardinality categorical column whose values split each
    #: series into one line per value. Empty for no split.
    split: str = ""
    orientation: str = "vertical"
    #: How duplicate X values are combined. "none" keeps every row,
    #: which is right for a scatter and wrong for a bar chart of a
    #: non-aggregated result.
    aggregation: str = "none"
    #: Per-type options. `stacked` applies to bar and area, the caps to
    #: pie and to everything else respectively.
    stacked: bool = False
    slice_cap: int = DEFAULT_SLICE_CAP
    point_cap: int = DEFAULT_POINT_CAP
    title: str = ""
    version: int = MODEL_VERSION

    def columns(self) -> tuple[str, ...]:
        """Every result column this spec names, x first."""
        names = ([self.x] if self.x else []) + list(self.series)
        if self.split:
            names.append(self.split)
        return tuple(names)


@dataclass(frozen=True)
class Series:
    """One drawn line, bar group or set of slices.

    `points` are (x, y) pairs: x is the raw value — a datetime for a
    temporal axis, a float for a numeric one, a string for a
    categorical one — and y is always a float. `column` and `split` say
    where the series came from, so the mapping bar (CORE-32) can name
    it without parsing `name`.
    """

    name: str
    points: tuple[tuple[Any, float], ...] = ()
    column: str = ""
    split: str = ""


@dataclass(frozen=True)
class ChartData:
    """What a spec applied to a result comes to.

    `reason` is non-empty exactly when there is nothing to draw, and is
    a sentence for the notice bar rather than an error code. `dropped`
    and `capped` are never zero silently: the counts are what the
    "Showing N of M" notice PG-04 established is built from.
    """

    series: tuple[Series, ...] = ()
    x_kind: str = CATEGORICAL
    y_kind: str = NUMERIC
    #: Categorical X values in draw order, so the renderer and the
    #: legend agree on the order without re-deriving it. Empty for a
    #: temporal or numeric axis.
    x_labels: tuple[str, ...] = ()
    #: Rows skipped: a NULL x or y, or a y that is not a number.
    dropped: int = 0
    #: Points or slices beyond the cap, folded into "Other" for a pie
    #: and left undrawn otherwise.
    capped: int = 0
    #: Rows that reached the chart, after aggregation.
    rows: int = 0
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.series) and not self.reason


@dataclass(frozen=True)
class Inference:
    """`infer`'s answer: a spec, or the reason there is none."""

    spec: ChartSpec | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.spec is not None


# Classification


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(
        value, bool
    )


def _is_temporal(value: Any) -> bool:
    return isinstance(value, (datetime, date, time))


def _as_float(value: Any) -> float | None:
    """`value` as a plottable number, or None when it is not one.

    Strings are parsed: a driver that hands back DECIMAL as text (and
    several do) should not make its column unchartable.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        try:
            return float(value)
        except (ValueError, OverflowError, InvalidOperation):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _declared_kind(text: str) -> str:
    """The class a declared type name implies, or "" for none."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return ""
    if any(hint in lowered for hint in _NOT_MEASURE_HINTS):
        return CATEGORICAL
    if any(hint in lowered for hint in _TEMPORAL_HINTS):
        return TEMPORAL
    if any(hint in lowered for hint in _NUMERIC_HINTS):
        return NUMERIC
    return ""


def _declared_types(provider: Any, table: str) -> dict[str, str]:
    """The declared types a provider can give, as name -> type text.

    `provider` is either a plain mapping (which is what a caller that
    already has `ColumnInfo`s builds) or a `MetadataProvider`, whose
    `column_kinds` names the same classes in its own vocabulary. A
    provider that raises — a dropped table, a closed connection — is
    simply one that told us nothing: classification then falls back to
    the values, which is never wrong, only less certain.
    """
    if provider is None:
        return {}
    if isinstance(provider, Mapping):
        return {str(k): str(v) for k, v in provider.items()}
    kinds = getattr(provider, "column_kinds", None)
    if callable(kinds):
        try:
            got = kinds(table)
        except Exception:
            return {}
        if isinstance(got, Mapping):
            return {str(k): str(v) for k, v in got.items()}
    return {}


def classify(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]] = (),
    *,
    provider: Any = None,
    table: str = "",
) -> dict[str, str]:
    """Label every column `temporal`, `numeric` or `categorical`.

    The declared type wins where there is one, because the catalog
    knows that an all-NULL column is a `timestamp`; the Python values
    decide otherwise. A column of mixed values is categorical unless
    every non-NULL value is of one plottable class — a column that is
    numbers *and* words is a label, not a measure. An all-NULL column
    with nothing declared is categorical too: it plots nothing either
    way, and calling it a measure would let `infer` pick an axis that
    draws an empty line.
    """
    declared = _declared_types(provider, table)
    seen: dict[str, list[int]] = {
        name: [0, 0, 0] for name in columns
    }  # numeric, temporal, other
    for row in rows:
        for index, name in enumerate(columns):
            if index >= len(row):
                continue
            value = row[index]
            if value is None:
                continue
            counts = seen[name]
            if _is_temporal(value):
                counts[1] += 1
            elif _is_number(value):
                counts[0] += 1
            else:
                counts[2] += 1

    classes: dict[str, str] = {}
    for name in columns:
        kind = _declared_kind(declared.get(name, ""))
        if kind:
            classes[name] = kind
            continue
        numeric, temporal, other = seen[name]
        if other or (numeric and temporal):
            classes[name] = CATEGORICAL
        elif temporal:
            classes[name] = TEMPORAL
        elif numeric:
            classes[name] = NUMERIC
        else:
            classes[name] = CATEGORICAL
    return classes


def _distinct(rows: Sequence[Sequence[Any]], index: int) -> int:
    values = set()
    for row in rows:
        if index < len(row):
            values.add(row[index])
        if len(values) > _SPLIT_CARDINALITY + 1:
            break
    return len(values)


# Inference


def infer(result: Any, classes: Mapping[str, str] | None = None) -> Inference:
    """The default mapping for `result`, or the reason there is none.

    The rule from RS-03, in order: the first temporal (else the first
    ordered) column is X, every numeric column that is not X is a
    series, and a single low-cardinality categorical column splits them.
    A result with no numeric column charts nothing and says so in
    words — "No numeric column to plot" beats an empty canvas.

    Chart type follows the axis: a temporal X gets a line, a
    categorical X a bar, two numerics a scatter.
    """
    columns = [str(c) for c in getattr(result, "columns", []) or []]
    rows = list(getattr(result, "rows", []) or [])
    if not columns:
        return Inference(reason=_("The result has no columns to chart."))
    if classes is None:
        classes = classify(columns, rows)

    numeric = [c for c in columns if classes.get(c) == NUMERIC]
    temporal = [c for c in columns if classes.get(c) == TEMPORAL]
    categorical = [c for c in columns if classes.get(c) == CATEGORICAL]
    if not numeric:
        return Inference(reason=_("No numeric column to plot."))

    if temporal:
        x, kind = temporal[0], "line"
    elif categorical:
        x, kind = categorical[0], "bar"
    elif len(numeric) >= 2:
        # Two measures and nothing to order them by: the question is
        # correlation, and that is a scatter of the first against the
        # second, not a line of both against the row number.
        return Inference(
            ChartSpec(
                type="scatter",
                x=numeric[0],
                series=(numeric[1],),
                aggregation="none",
            )
        )
    else:
        x, kind = "", "line"

    series = tuple(c for c in numeric if c != x)
    if not series:
        return Inference(reason=_("No numeric column to plot."))

    split = ""
    others = [c for c in categorical if c != x]
    if len(others) == 1 and 1 < _distinct(rows, columns.index(others[0])) <= (
        _SPLIT_CARDINALITY
    ):
        split = others[0]

    # A categorical X almost always comes from a GROUP BY, but not
    # always: summing duplicates beats drawing one bar per row.
    aggregation = "sum" if kind == "bar" else "none"
    return Inference(
        ChartSpec(type=kind, x=x, series=series, split=split, aggregation=aggregation)
    )


# Validation


def validate(spec: ChartSpec, columns: Sequence[str] = ()) -> list[str]:
    """Everything wrong with `spec`, as sentences for the notice bar.

    An empty list means it can be drawn. Nothing here raises: a spec
    restored from an older workspace is expected to be wrong sometimes,
    and the user is owed the reason, not a traceback.
    """
    problems: list[str] = []
    if spec.type not in CHART_TYPES:
        problems.append(
            _("Unknown chart type “%s”.") % spec.type
        )
    if spec.aggregation not in AGGREGATIONS:
        problems.append(
            _("Unknown aggregation “%s”.") % spec.aggregation
        )
    if spec.orientation not in ORIENTATIONS:
        problems.append(
            _("Unknown orientation “%s”.") % spec.orientation
        )
    if not spec.series:
        problems.append(_("No column is mapped to a value axis."))
    if spec.type == "pie" and len(spec.series) > 1:
        problems.append(_("A pie chart draws one value column, not several."))
    if columns:
        known = set(columns)
        for name in spec.columns():
            if name not in known:
                problems.append(
                    _("The result has no column named “%s”.") % name
                )
    return problems


# Mapping rows onto series


def _aggregate(values: list[float], how: str) -> float:
    if how == "count":
        return float(len(values))
    if how == "sum":
        return float(sum(values))
    if how == "avg":
        return float(sum(values) / len(values)) if values else 0.0
    if how == "min":
        return float(min(values))
    if how == "max":
        return float(max(values))
    return float(values[-1])


def _label(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return _("Yes") if value else _("No")
    return str(value)


def _sort_key(value: Any) -> tuple[int, Any]:
    """Order a temporal or numeric X. Naive and aware datetimes never
    compare, so they are separated rather than allowed to raise."""
    if isinstance(value, datetime):
        return (1 if value.tzinfo else 0, value.timestamp() if value.tzinfo else value)
    if isinstance(value, (date, time)):
        return (0, value)
    number = _as_float(value)
    return (0, number if number is not None else 0.0)


def series_from(
    spec: ChartSpec,
    result: Any,
    classes: Mapping[str, str] | None = None,
) -> ChartData:
    """`spec` applied to `result`: named series of (x, y) pairs.

    Rows with a NULL x, a NULL y, or a y that is not a number are
    dropped and counted; duplicate X values are combined with the
    spec's aggregation; a pie folds everything past `slice_cap` into
    one "Other" slice and every other type stops at `point_cap`. Both
    counts come back on the `ChartData`, because a chart of a partial
    result that does not say so is a lie.

    A spec that cannot be applied — an unknown type, a column the
    result no longer has — comes back as a `ChartData` with a `reason`
    and no series. This never raises.
    """
    columns = [str(c) for c in getattr(result, "columns", []) or []]
    rows = list(getattr(result, "rows", []) or [])
    problems = validate(spec, columns)
    if problems:
        return ChartData(reason=" ".join(problems))
    if classes is None:
        classes = classify(columns, rows)

    index = {name: i for i, name in enumerate(columns)}
    x_index = index[spec.x] if spec.x else -1
    split_index = index[spec.split] if spec.split else -1
    x_kind = classes.get(spec.x, NUMERIC) if spec.x else NUMERIC
    if spec.type == "pie":
        # A pie's X is always a label, whatever the column's type.
        x_kind = CATEGORICAL

    dropped = 0
    # (column, split label) -> x key -> [values], preserving first-seen
    # order for a categorical axis.
    buckets: dict[tuple[str, str], dict[Any, list[float]]] = {}
    x_order: list[Any] = []
    seen_x: set[Any] = set()

    for position, row in enumerate(rows):
        if x_index >= 0:
            if x_index >= len(row):
                dropped += 1
                continue
            x_value = row[x_index]
            if x_value is None:
                dropped += 1
                continue
            if x_kind == CATEGORICAL:
                x_value = _label(x_value)
            elif x_kind == NUMERIC:
                number = _as_float(x_value)
                if number is None:
                    dropped += 1
                    continue
                x_value = number
        else:
            # No X column: the row number is the axis, which is what a
            # result with nothing ordered to plot against gets.
            x_value = float(position)
        split_label = ""
        if split_index >= 0 and split_index < len(row):
            split_label = _label(row[split_index])

        used = False
        for name in spec.series:
            column = index[name]
            value = row[column] if column < len(row) else None
            number = _as_float(value)
            if number is None:
                continue
            used = True
            key = (name, split_label)
            per_x = buckets.setdefault(key, {})
            per_x.setdefault(x_value, []).append(number)
        if not used:
            dropped += 1
            continue
        try:
            if x_value not in seen_x:
                seen_x.add(x_value)
                x_order.append(x_value)
        except TypeError:  # pragma: no cover - unhashable x value
            x_order.append(x_value)

    if not buckets:
        return ChartData(
            x_kind=x_kind,
            dropped=dropped,
            reason=_("Nothing to plot: no row has a value in the chosen columns."),
        )

    if x_kind in (TEMPORAL, NUMERIC):
        try:
            x_order.sort(key=_sort_key)
        except TypeError:  # pragma: no cover - values that never compare
            pass

    multi = len(spec.series) > 1
    capped = 0
    plotted = 0
    built: list[Series] = []
    for (name, split_label), per_x in buckets.items():
        points: list[tuple[Any, float]] = []
        for x_value in x_order:
            values = per_x.get(x_value)
            if values is None:
                continue
            if spec.aggregation == "none" and len(values) > 1:
                # "none" means every row is its own point, so the
                # duplicates stay — a scatter's point count is the
                # message.
                points.extend((x_value, v) for v in values)
            else:
                points.append(
                    (
                        x_value,
                        _aggregate(values, spec.aggregation)
                        if spec.aggregation != "none"
                        else values[0],
                    )
                )
        if not points:
            continue
        if spec.type == "pie":
            points, folded = _cap_slices(points, spec.slice_cap)
            capped += folded
        else:
            cap = max(int(spec.point_cap or 0), 0)
            if cap and len(points) > cap:
                capped += len(points) - cap
                points = points[:cap]
        plotted += len(points)
        if split_label and multi:
            label = _("%(column)s — %(split)s") % {
                "column": name,
                "split": split_label,
            }
        elif split_label:
            label = split_label
        else:
            label = name
        built.append(
            Series(
                name=label,
                points=tuple(points),
                column=name,
                split=split_label,
            )
        )

    labels: tuple[str, ...] = ()
    if x_kind == CATEGORICAL:
        drawn = {p[0] for s in built for p in s.points}
        labels = tuple(x for x in x_order if x in drawn)
        other = _(OTHER_LABEL)
        if any(p[0] == other for s in built for p in s.points):
            labels = tuple(x for x in labels if x != other) + (other,)

    return ChartData(
        series=tuple(built),
        x_kind=x_kind,
        y_kind=NUMERIC,
        x_labels=labels,
        dropped=dropped,
        capped=capped,
        rows=plotted,
    )


def _cap_slices(
    points: list[tuple[Any, float]], cap: int
) -> tuple[list[tuple[Any, float]], int]:
    """The `cap` largest slices, with the tail summed into one "Other".

    The folded slices are still *drawn*, just together: dropping them
    would make the pie add up to less than the whole, which is the one
    thing a pie must never do.
    """
    limit = max(int(cap or 0), 0)
    if not limit or len(points) <= limit:
        return points, 0
    ordered = sorted(points, key=lambda p: p[1], reverse=True)
    head, tail = ordered[: limit - 1], ordered[limit - 1 :]
    head.append((_(OTHER_LABEL), float(sum(v for _x, v in tail))))
    return head, len(tail)


# Serialisation (CORE-33 persists these dicts into TabState and onto
# saved queries)


def to_dict(spec: ChartSpec) -> dict:
    """`spec` as plain JSON-able data."""
    return {
        "version": spec.version,
        "type": spec.type,
        "x": spec.x,
        "series": list(spec.series),
        "split": spec.split,
        "orientation": spec.orientation,
        "aggregation": spec.aggregation,
        "stacked": spec.stacked,
        "slice_cap": spec.slice_cap,
        "point_cap": spec.point_cap,
        "title": spec.title,
    }


def _int(value: Any, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


def from_dict(data: Any) -> ChartSpec:
    """The inverse of `to_dict`, tolerant of missing and unknown keys.

    An unknown chart type or aggregation is *kept* rather than
    corrected or rejected: `validate` and `series_from` then report it
    in words, which is the difference between "chart not restored" and
    a dialog. Only the shape is normalised here.
    """
    data = data if isinstance(data, Mapping) else {}
    series = data.get("series")
    if isinstance(series, str):
        series = [series]
    if not isinstance(series, (list, tuple)):
        series = []
    return ChartSpec(
        type=str(data.get("type", "line")),
        x=str(data.get("x", "") or ""),
        series=tuple(str(name) for name in series if str(name)),
        split=str(data.get("split", "") or ""),
        orientation=str(data.get("orientation", "vertical") or "vertical"),
        aggregation=str(data.get("aggregation", "none") or "none"),
        stacked=bool(data.get("stacked", False)),
        slice_cap=_int(data.get("slice_cap"), DEFAULT_SLICE_CAP),
        point_cap=_int(data.get("point_cap"), DEFAULT_POINT_CAP),
        title=str(data.get("title", "") or ""),
        version=_int(data.get("version"), MODEL_VERSION),
    )


def dump_state(spec: ChartSpec) -> str:
    """`spec` as the JSON text a TabState or a saved query carries."""
    return json.dumps({"version": MODEL_VERSION, "chart": to_dict(spec)})


def load_state(text: str) -> ChartSpec | None:
    """The inverse of `dump_state`, and deliberately unexcitable:
    empty text, malformed JSON, a version from the future or a payload
    that is not a mapping all come back as None, which callers read as
    "no saved chart", never as an error. A workspace must always open.
    """
    if not text:
        return None
    try:
        envelope = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, Mapping):
        return None
    try:
        version = int(envelope.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version > MODEL_VERSION or version < 1:
        return None
    data = envelope.get("chart")
    if not isinstance(data, Mapping):
        return None
    try:
        return from_dict(data)
    except (AttributeError, TypeError, ValueError):
        return None
