"""Dashboards: several saved charts, laid out together (CORE-35).

Once a saved query can carry a chart (CORE-33), a dashboard is only a
place to put several of them at once. That place is a file:

    <config>/dashboards/<id>.toml

    id = "..."
    name = "Signups"
    connection = "prod"     # ConnectionProfile.name in the workspace
    columns = 2             # cells per row
    interval = 0            # seconds between refreshes; 0 is off
    version = 1

    [[cell]]
    query = "Weekly signups"   # SavedItem.name in backend/saved.py
    title = ""                 # optional override of the query's name
    width = 1                  # columns spanned
    height = 1                 # row units

One TOML file per dashboard, written through tomlwrite.merge() like
every other config file, because a dashboard's definition — its name,
its layout, the queries it names — is configuration, not session churn
(CORE-13): it is worth reading in a diff and worth committing. The
*open tab* is not stored here at all; `TabState.dashboard` is just this
file's id.

Three rules this module keeps, the same three the rest of the config
layer does:

- **A cell references a saved query by name, never by copy.** The SQL
  and the chart live in `saved_queries.json`; editing the query there
  changes every dashboard that names it, and a name that is gone is
  reported by `bind()` rather than dropped.
- **Report, never raise.** A hand-edited file with a bad number, a
  missing name or an unknown key is recorded through
  `config.record_error` and replaced by the default. A dashboard must
  always open.
- **No frontend.** Nothing here imports GTK; the layout is arithmetic
  over cell widths, so it is testable without a display.

The module-level `store` is the single instance; tabs subscribe to
follow a change made in another window or by hand on disk, and must
unsubscribe on teardown.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from sqlide.backend import config, tomlwrite
from sqlide.backend.db import metrics
from sqlide.backend.saved import SavedItem
from sqlide.i18n import _

MODEL_VERSION = 1

#: Grid bounds. Deliberately small numbers: a dashboard whose cells are
#: too narrow to read a legend in is not a dashboard.
MIN_COLUMNS = 1
MAX_COLUMNS = 4
DEFAULT_COLUMNS = 2
MAX_CELL_HEIGHT = 3

#: One row unit, in pixels. The frontend multiplies a cell's `height`
#: by this; it lives here so the layout is one number, not two.
ROW_HEIGHT = 260

#: Refresh off by default: a dashboard someone opens to look at once
#: should not start polling a production server on its own.
INTERVAL_OFF = 0


def clamp_interval(seconds: int) -> int:
    """A refresh interval held to its range. 0 means "no interval, use
    the Refresh button"; anything else goes through the same clamp the
    monitoring dashboard applies (backend/db/metrics.py), so the two
    cannot drift and neither can be hand-edited into a busy loop."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return INTERVAL_OFF
    if seconds <= 0:
        return INTERVAL_OFF
    return metrics.clamp_interval(seconds)


def _clamp(value, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


@dataclass
class DashboardCell:
    """One cell: a saved query, and how much room it gets."""

    #: SavedItem.name in the global saved-queries store. The cell shows
    #: that query's chart; a name nothing matches is reported in place.
    query: str = ""
    #: Shown instead of the query's name when set, so a cell can be
    #: called "Signups this week" without renaming the query.
    title: str = ""
    width: int = 1
    height: int = 1

    def label(self) -> str:
        return self.title.strip() or self.query

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> DashboardCell:
        def text(key: str) -> str:
            got = data.get(key, "")
            return got if isinstance(got, str) else str(got)

        if path is not None:
            for key in data:
                if key not in cls.__dataclass_fields__:
                    config.record_error(
                        path, "unknown key for a cell, ignored", key=key
                    )
        return cls(
            query=text("query"),
            title=text("title"),
            width=_clamp(data.get("width", 1), 1, MAX_COLUMNS, 1),
            height=_clamp(data.get("height", 1), 1, MAX_CELL_HEIGHT, 1),
        )


@dataclass
class Dashboard:
    name: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: The one connection every cell runs on. Named, not resolved: the
    #: file is global and the profile lives in a workspace.
    connection: str = ""
    columns: int = DEFAULT_COLUMNS
    interval: int = INTERVAL_OFF
    cells: list[DashboardCell] = field(default_factory=list)
    version: int = MODEL_VERSION

    # Layout

    def normalise(self) -> None:
        """Pull every number back into range. Called after a load and
        after any edit, so no caller has to remember to."""
        self.columns = _clamp(
            self.columns, MIN_COLUMNS, MAX_COLUMNS, DEFAULT_COLUMNS
        )
        self.interval = clamp_interval(self.interval)
        for cell in self.cells:
            cell.width = _clamp(cell.width, 1, self.columns, 1)
            cell.height = _clamp(cell.height, 1, MAX_CELL_HEIGHT, 1)

    def move(self, index: int, offset: int) -> bool:
        """Reorder: the cell at `index` moved `offset` places. Order in
        the list is order on the grid, so this is the whole of it."""
        target = index + offset
        if not (0 <= index < len(self.cells) and 0 <= target < len(self.cells)):
            return False
        self.cells.insert(target, self.cells.pop(index))
        return True

    def add_cell(self, query: str, title: str = "") -> DashboardCell:
        cell = DashboardCell(query=query, title=title)
        self.cells.append(cell)
        self.normalise()
        return cell

    def remove_cell(self, cell: DashboardCell) -> None:
        if cell in self.cells:
            self.cells.remove(cell)

    def to_dict(self) -> dict:
        data = asdict(self)
        cells = data.pop("cells")
        # Omitted rather than written as an empty array: tomlwrite
        # renders `cell = []` as a scalar, which then collides with the
        # [[cell]] tables the first added cell writes.
        if cells:
            data["cell"] = cells
        return data

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> Dashboard:
        """One dashboard off disk. Every value is coerced and clamped;
        a broken one costs its own default, never the whole file."""
        entries = data.get("cell", [])
        if not isinstance(entries, list):
            if path is not None:
                config.record_error(
                    path, "cell must be a list of [[cell]] tables", key="cell"
                )
            entries = []
        known = set(cls.__dataclass_fields__) | {"cell"}
        if path is not None:
            for key in data:
                if key not in known and key != "cells":
                    config.record_error(
                        path, "unknown key for a dashboard, ignored", key=key
                    )

        def text(key: str) -> str:
            got = data.get(key, "")
            return got if isinstance(got, str) else str(got)

        dashboard = cls(
            id=text("id") or uuid.uuid4().hex,
            name=text("name"),
            connection=text("connection"),
            columns=_clamp(
                data.get("columns", DEFAULT_COLUMNS),
                MIN_COLUMNS,
                MAX_COLUMNS,
                DEFAULT_COLUMNS,
            ),
            interval=clamp_interval(data.get("interval", INTERVAL_OFF)),
            cells=[
                DashboardCell.from_dict(entry, path=path)
                for entry in entries
                if isinstance(entry, dict)
            ],
            version=_clamp(data.get("version", MODEL_VERSION), 1, 99, MODEL_VERSION),
        )
        dashboard.normalise()
        return dashboard


@dataclass(frozen=True)
class Bound:
    """A cell paired with the saved query it names, or the reason it
    could not be. A cell is never silently dropped: a deleted query is
    a sentence inside the cell, and the other cells still refresh."""

    cell: DashboardCell
    item: SavedItem | None = None
    problem: str = ""

    @property
    def sql(self) -> str:
        return self.item.sql if self.item is not None else ""

    @property
    def chart(self) -> str:
        return self.item.chart if self.item is not None else ""


def bind(
    dashboard: Dashboard, items: Iterable[SavedItem]
) -> list[Bound]:
    """Every cell matched against the saved queries that exist now."""
    by_name = {item.name: item for item in items}
    bound = []
    for cell in dashboard.cells:
        item = by_name.get(cell.query)
        if item is None:
            problem = _(
                "The saved query “%s” no longer exists."
            ) % (cell.query or _("(unnamed)"))
            bound.append(Bound(cell=cell, problem=problem))
        elif not item.sql.strip():
            bound.append(
                Bound(cell=cell, item=item, problem=_("This saved query is empty."))
            )
        else:
            bound.append(Bound(cell=cell, item=item))
    return bound


@dataclass(frozen=True)
class Placement:
    """Where one cell sits on the grid, in grid units."""

    index: int
    column: int
    row: int
    width: int
    height: int


def layout(dashboard: Dashboard) -> list[Placement]:
    """Cells packed left to right, wrapping when the next one does not
    fit in the row's remaining width. Order in the list is the layout,
    so reordering needs no coordinates to keep valid — and a file
    hand-edited to a smaller `columns` still lays out."""
    columns = _clamp(dashboard.columns, MIN_COLUMNS, MAX_COLUMNS, DEFAULT_COLUMNS)
    placements: list[Placement] = []
    column = 0
    row = 0
    row_height = 1
    for index, cell in enumerate(dashboard.cells):
        width = _clamp(cell.width, 1, columns, 1)
        height = _clamp(cell.height, 1, MAX_CELL_HEIGHT, 1)
        if column and column + width > columns:
            row += row_height
            column = 0
            row_height = 1
        placements.append(
            Placement(index=index, column=column, row=row, width=width, height=height)
        )
        column += width
        row_height = max(row_height, height)
        if column >= columns:
            row += row_height
            column = 0
            row_height = 1
    return placements


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(name: str) -> str:
    """A file-name stem from a dashboard's name, so the directory reads
    as a list of dashboards rather than a list of hex ids."""
    stem = _SLUG.sub("-", name.strip().lower()).strip("-")
    return stem[:48] or "dashboard"


class DashboardStore:
    """The dashboards/ directory: one TOML file each."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory
        self.dashboards: list[Dashboard] = []
        self._paths: dict[str, Path] = {}
        self._loaded = False
        self._listeners: list[Callable[[list[Dashboard]], None]] = []

    @property
    def directory(self) -> Path:
        # Resolved late: the config directory can be redirected (the
        # --config-dir flag, a test) after this module is imported.
        return self._directory or (config.config_dir() / "dashboards")

    def load(self) -> list[Dashboard]:
        if not self._loaded:
            self._loaded = True
            self._read()
        return self.dashboards

    def reload(self) -> list[Dashboard]:
        """Re-read the directory and notify subscribers — what a file
        watcher fires, so a dashboard edited by hand shows up."""
        self._loaded = True
        self._read()
        self._notify()
        return self.dashboards

    def _read(self) -> None:
        self.dashboards = []
        self._paths = {}
        directory = self.directory
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.toml")):
            data = config.load_toml(path)
            if not data:
                continue
            dashboard = Dashboard.from_dict(data, path=path)
            if not dashboard.name:
                dashboard.name = path.stem
            self.dashboards.append(dashboard)
            self._paths[dashboard.id] = path
            self._watch(path)
        self.dashboards.sort(key=lambda d: d.name.lower())

    def _watch(self, path: Path) -> None:
        """Follow one file. Re-registered rather than added to, so a
        reload does not leave two callbacks on the same path."""
        config.watcher.unwatch(path)
        config.watcher.watch(path, lambda _path: self.reload())

    def get(self, dashboard_id: str) -> Dashboard | None:
        return next(
            (d for d in self.load() if d.id == dashboard_id), None
        )

    def path_for(self, dashboard: Dashboard) -> Path:
        existing = self._paths.get(dashboard.id)
        if existing is not None:
            return existing
        stem = slug(dashboard.name)
        taken = {p.name for p in self._paths.values()}
        candidate = f"{stem}.toml"
        n = 2
        while candidate in taken or (self.directory / candidate).exists():
            candidate = f"{stem}-{n}.toml"
            n += 1
        return self.directory / candidate

    def unique_name(self, name: str, *, exclude: str = "") -> str:
        """A name no other dashboard is using — the same "(2)" rule the
        saved-SQL store and the workspace's connections follow."""
        name = name.strip() or _("Dashboard")
        taken = {d.name for d in self.load() if d.id != exclude}
        if name not in taken:
            return name
        n = 2
        while f"{name} ({n})" in taken:
            n += 1
        return f"{name} ({n})"

    def create(self, name: str, connection: str = "") -> Dashboard:
        self.load()
        dashboard = Dashboard(
            name=self.unique_name(name), connection=connection
        )
        self.dashboards.append(dashboard)
        self.save(dashboard)
        return dashboard

    def save(self, dashboard: Dashboard) -> Path:
        """Write one dashboard's file, merged over what is there so a
        comment someone left survives a change made in the UI."""
        self.load()
        dashboard.normalise()
        if dashboard not in self.dashboards:
            self.dashboards.append(dashboard)
        path = self.path_for(dashboard)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        path.write_text(
            tomlwrite.merge(existing, dashboard.to_dict()), encoding="utf-8"
        )
        config.watcher.forget(path)
        self._watch(path)
        self._paths[dashboard.id] = path
        self.dashboards.sort(key=lambda d: d.name.lower())
        self._notify()
        return path

    def remove(self, dashboard: Dashboard) -> None:
        self.load()
        path = self._paths.pop(dashboard.id, None)
        self.dashboards = [d for d in self.dashboards if d.id != dashboard.id]
        if path is not None:
            config.watcher.unwatch(path)
            try:
                path.unlink()
            except OSError as exc:
                config.record_error(path, f"could not be removed ({exc})")
        self._notify()

    # Subscriptions

    def subscribe(self, listener: Callable[[list[Dashboard]], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[list[Dashboard]], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener(self.dashboards)


store = DashboardStore()


def chartable(items: Sequence[SavedItem]) -> list[SavedItem]:
    """The saved queries a dashboard cell can be made from: the ones
    that carry a chart (CORE-33). A query without one would draw an
    empty cell, so it is not offered."""
    return [item for item in items if item.chart.strip()]
