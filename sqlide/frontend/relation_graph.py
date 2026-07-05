"""Relation graph tab: tables as nodes, foreign keys as edges.

Layout and rendering are delegated to Graphviz (the `dot` CLI): the
connector's list_tables()/list_columns()/list_relations() output turns
into a DOT document with one HTML-table node per database table (PK
columns bold, every column a port so edges attach column-to-column),
`dot -Tpng` renders it, and the result shows in a scrollable picture.
Zoom re-renders through Graphviz at a different DPI, so the image
stays crisp instead of scaling pixels. Colors follow the app's
light/dark style at render time; Refresh re-reads both the catalog
and the style. A hint replaces the graph when `dot` is not installed.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from gi.repository import Adw, Gdk, GLib, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import (
    ColumnInfo,
    Connector,
    ConnectorError,
    RelationInfo,
)
from sqlide.backend.workspaces import TabState
from sqlide.frontend.util import run_async

_BASE_DPI = 96
_ZOOM_STEPS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)


class RelationGraphTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error
        self._dot_source = ""  # kept for DPI-only re-renders (zoom)
        self._zoom_index = _ZOOM_STEPS.index(1.0)

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Reload tables and relations")
        refresh.connect("clicked", lambda *_: self.reload())
        zoom_box = Gtk.Box()
        zoom_box.add_css_class("linked")
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic")
        zoom_out.set_tooltip_text("Zoom out")
        zoom_out.connect("clicked", lambda *_: self._step_zoom(-1))
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic")
        zoom_in.set_tooltip_text("Zoom in")
        zoom_in.connect("clicked", lambda *_: self._step_zoom(1))
        zoom_box.append(zoom_out)
        zoom_box.append(zoom_in)
        self._zoom_label = Gtk.Label(label="100%")
        self._zoom_label.add_css_class("dim-label")
        self._status = Gtk.Label(xalign=1, hexpand=True)
        self._status.add_css_class("dim-label")
        bar.append(refresh)
        bar.append(zoom_box)
        bar.append(self._zoom_label)
        bar.append(self._status)
        self.append(bar)

        self._picture = Gtk.Picture(
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER
        )
        self._picture.set_can_shrink(False)
        self._scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._scroller.set_child(self._picture)

        self._placeholder = Adw.StatusPage(
            icon_name="network-workgroup-symbolic",
            title="No Relations",
            vexpand=True,
        )
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(self._scroller, "graph")
        self._stack.add_named(self._placeholder, "message")
        self.append(self._stack)

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(kind="relations", connection=self.profile.name)

    def reload(self) -> None:
        self._status.set_text("Loading…")
        dark = Adw.StyleManager.get_default().get_dark()

        def work():
            connector = self._ensure(self.profile)
            tables = [
                t.name for t in connector.list_tables() if t.kind == "table"
            ]
            columns = {name: connector.list_columns(name) for name in tables}
            relations = connector.list_relations()
            dot = _build_dot(tables, columns, relations, dark=dark)
            return dot, _render(dot, self._zoom()), len(tables), len(relations)

        def done(result) -> None:
            dot, png, n_tables, n_relations = result
            self._dot_source = dot
            if not n_tables:
                self._show_message(
                    "No Relations", "The connected database has no tables."
                )
                self._status.set_text("")
                return
            self._set_png(png)
            self._status.set_text(
                f"{n_tables} table(s) · {n_relations} foreign key(s)"
            )

        def failed(exc: Exception) -> None:
            self._status.set_text("")
            self._show_message("Could Not Render", str(exc))
            if not isinstance(exc, _DotMissingError):
                self._show_error(str(exc))

        run_async(work, done, failed)

    # Zoom

    def _zoom(self) -> float:
        return _ZOOM_STEPS[self._zoom_index]

    def _step_zoom(self, delta: int) -> None:
        index = min(max(self._zoom_index + delta, 0), len(_ZOOM_STEPS) - 1)
        if index == self._zoom_index or not self._dot_source:
            return
        self._zoom_index = index
        self._zoom_label.set_text(f"{round(self._zoom() * 100)}%")
        dot = self._dot_source

        def work():
            return _render(dot, self._zoom())

        run_async(
            work, self._set_png, lambda exc: self._show_error(str(exc))
        )

    # Rendering

    def _set_png(self, png: bytes) -> None:
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
        except GLib.Error as exc:
            self._show_message("Could Not Render", str(exc))
            return
        self._picture.set_paintable(texture)
        self._stack.set_visible_child_name("graph")

    def _show_message(self, title: str, description: str) -> None:
        self._placeholder.set_title(title)
        self._placeholder.set_description(description)
        self._stack.set_visible_child_name("message")


class _DotMissingError(ConnectorError):
    pass


def _render(dot: str, zoom: float) -> bytes:
    """Run the DOT document through Graphviz at the zoom's DPI."""
    try:
        completed = subprocess.run(
            ["dot", "-Tpng", f"-Gdpi={round(_BASE_DPI * zoom)}"],
            input=dot.encode(),
            capture_output=True,
        )
    except FileNotFoundError:
        raise _DotMissingError(
            "Graphviz is not installed — install it (the `dot` command) "
            "to render the relation graph."
        ) from None
    if completed.returncode != 0:
        raise ConnectorError(
            "Graphviz failed: "
            + completed.stderr.decode(errors="replace").strip()
        )
    return completed.stdout


def _build_dot(
    tables: list[str],
    columns: dict[str, list[ColumnInfo]],
    relations: list[RelationInfo],
    dark: bool,
) -> str:
    """DOT document: one HTML-table node per table (header row + one
    port row per column, PK bold), one edge per foreign-key column."""
    if dark:
        fg, border, header_bg, row_bg, edge = (
            "#eeeeec", "#5e5c64", "#3d3846", "#241f31", "#9a9996",
        )
    else:
        fg, border, header_bg, row_bg, edge = (
            "#241f31", "#9a9996", "#deddda", "#ffffff", "#5e5c64",
        )
    lines = [
        "digraph relations {",
        "  rankdir=LR;",
        "  bgcolor=transparent;",
        "  nodesep=0.5; ranksep=0.9;",
        f'  node [shape=none, fontname="Sans", fontsize=10, fontcolor="{fg}"];',
        f'  edge [color="{edge}", arrowsize=0.7];',
    ]
    for table in tables:
        rows = [
            f'<TR><TD BGCOLOR="{header_bg}" ALIGN="CENTER">'
            f"<B>{_escape(table)}</B></TD></TR>"
        ]
        for column in columns.get(table, []):
            text = _escape(f"{column.name}  {column.type}".rstrip())
            if column.is_pk:
                text = f"<B>{text}</B>"
            rows.append(
                f'<TR><TD BGCOLOR="{row_bg}" ALIGN="LEFT" '
                f'PORT="{_escape(column.name)}">{text}</TD></TR>'
            )
        label = (
            f'<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" '
            f'CELLPADDING="4" COLOR="{border}">{"".join(rows)}</TABLE>>'
        )
        lines.append(f"  {_quote(table)} [label={label}];")
    known = set(tables)
    for rel in relations:
        if rel.table not in known or rel.ref_table not in known:
            continue
        source = f"{_quote(rel.table)}:{_quote(rel.column)}"
        target = _quote(rel.ref_table)
        if rel.ref_column:
            target += f":{_quote(rel.ref_column)}"
        lines.append(f"  {source} -> {target};")
    lines.append("}")
    return "\n".join(lines)


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
