"""Relation graph tab: tables as nodes, foreign keys as edges.

The graph is an interactive canvas (Gtk.DrawingArea + cairo): every
table is a node showing its columns (PK bold), every foreign key an
edge attached to the referencing and referenced column rows. Nodes can
be dragged to rearrange the diagram; edges follow live. Zoom scales
the whole drawing (crisp, since it re-draws through cairo).

Only the *initial* placement is delegated to Graphviz: node sizes are
measured with Pango, handed to `dot -Tplain` as fixed-size boxes, and
the returned positions seed the canvas. Without the `dot` binary the
nodes start on a simple grid instead — moving them still works.
Colors follow the app's light/dark style at draw time; Refresh
re-reads the catalog and recomputes the layout (discarding manual
positions).
"""

from __future__ import annotations

import math
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import ColumnInfo, Connector, RelationInfo
from sqlide.backend.workspaces import TabState
from sqlide.frontend.util import run_async

_ZOOM_STEPS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)

_FONT = "Sans 10"
_PAD_X = 8  # text padding inside a row
_PAD_Y = 3
_MARGIN = 24  # canvas margin around the graph
_GRID_GAP = 40  # fallback layout spacing


@dataclass
class _Node:
    """One table on the canvas; x/y is the top-left corner in canvas
    (unzoomed) coordinates."""

    name: str
    columns: list[ColumnInfo]
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    header_height: float = 0.0
    row_height: float = 0.0

    @property
    def height(self) -> float:
        return self.header_height + self.row_height * len(self.columns)

    def row_anchor_y(self, column: str) -> float:
        """Vertical center of a column's row (header center if the
        column is unknown), in canvas coordinates."""
        for i, info in enumerate(self.columns):
            if info.name == column:
                return self.y + self.header_height + (i + 0.5) * self.row_height
        return self.y + self.header_height / 2


@dataclass
class _Palette:
    fg: str
    border: str
    header_bg: str
    row_bg: str
    edge: str


def _palette(dark: bool) -> _Palette:
    if dark:
        return _Palette("#eeeeec", "#5e5c64", "#3d3846", "#241f31", "#9a9996")
    return _Palette("#241f31", "#9a9996", "#deddda", "#ffffff", "#5e5c64")


def _rgb(hex_color: str) -> tuple[float, float, float]:
    return tuple(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))


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
        self._zoom_index = _ZOOM_STEPS.index(1.0)
        self._nodes: list[_Node] = []  # draw order; last is topmost
        self._relations: list[RelationInfo] = []
        self._drag_node: _Node | None = None
        self._drag_origin = (0.0, 0.0)

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text(
            "Reload tables and relations (recomputes the layout)"
        )
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

        self._canvas = Gtk.DrawingArea()
        self._canvas.set_draw_func(self._draw)
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._canvas.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self._canvas.add_controller(motion)

        self._scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._scroller.set_child(self._canvas)

        self._placeholder = Adw.StatusPage(
            icon_name="network-workgroup-symbolic",
            title="No Relations",
            vexpand=True,
        )
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(self._scroller, "graph")
        self._stack.add_named(self._placeholder, "message")
        self.append(self._stack)

        # Redraw with the other palette when the app style flips; the
        # StyleManager is a singleton, so drop the handler with the tab.
        style = Adw.StyleManager.get_default()
        handler = style.connect(
            "notify::dark", lambda *_: self._canvas.queue_draw()
        )
        self._canvas.connect(
            "destroy", lambda *_: style.disconnect(handler)
        )

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(kind="relations", connection=self.profile.name)

    # Loading

    def reload(self) -> None:
        self._status.set_text("Loading…")

        def work():
            connector = self._ensure(self.profile)
            tables = [
                t.name for t in connector.list_tables() if t.kind == "table"
            ]
            columns = {name: connector.list_columns(name) for name in tables}
            relations = [
                r
                for r in connector.list_relations()
                if r.table in columns and r.ref_table in columns
            ]
            return tables, columns, relations

        def done(result) -> None:
            tables, columns, relations = result
            if not tables:
                self._show_message(
                    "No Relations", "The connected database has no tables."
                )
                self._status.set_text("")
                return
            self._nodes = [_Node(name, columns[name]) for name in tables]
            self._relations = relations
            self._measure_nodes()
            self._layout_nodes()
            self._resize_canvas()
            self._stack.set_visible_child_name("graph")
            self._canvas.queue_draw()
            self._status.set_text(
                f"{len(tables)} table(s) · {len(relations)} foreign key(s)"
                " · drag tables to rearrange"
            )

        def failed(exc: Exception) -> None:
            self._status.set_text("")
            self._show_message("Could Not Load", str(exc))
            self._show_error(str(exc))

        run_async(work, done, failed)

    # Geometry

    def _pango_layout(self) -> Pango.Layout:
        layout = self._canvas.create_pango_layout("")
        layout.set_font_description(Pango.FontDescription.from_string(_FONT))
        return layout

    def _measure_nodes(self) -> None:
        """Size every node from its rendered text (must run on the main
        thread — Pango layouts come from the widget)."""
        layout = self._pango_layout()

        def text_size(text: str, bold: bool = False) -> tuple[float, float]:
            layout.set_text(text, -1)
            attrs = Pango.AttrList()
            if bold:
                attr = Pango.attr_weight_new(Pango.Weight.BOLD)
                attrs.insert(attr)
            layout.set_attributes(attrs)
            _ink, logical = layout.get_pixel_extents()
            return logical.width, logical.height

        for node in self._nodes:
            width, header_h = text_size(node.name, bold=True)
            row_h = header_h
            for column in node.columns:
                w, h = text_size(_column_text(column), bold=column.is_pk)
                width = max(width, w)
                row_h = max(row_h, h)
            node.width = width + 2 * _PAD_X
            node.header_height = header_h + 2 * _PAD_Y
            node.row_height = row_h + 2 * _PAD_Y

    def _layout_nodes(self) -> None:
        """Initial placement: Graphviz positions when `dot` exists,
        otherwise a plain grid."""
        positions = _dot_positions(self._nodes, self._relations)
        if positions is not None:
            for node in self._nodes:
                node.x, node.y = positions.get(node.name, (0.0, 0.0))
        else:
            self._grid_layout()
        self._normalize_origin()

    def _grid_layout(self) -> None:
        """Rows of nodes, roughly square overall."""
        per_row = max(1, round(math.sqrt(len(self._nodes))))
        x = y = 0.0
        row_bottom = 0.0
        for i, node in enumerate(self._nodes):
            if i and i % per_row == 0:
                x = 0.0
                y = row_bottom + _GRID_GAP
            node.x, node.y = x, y
            x += node.width + _GRID_GAP
            row_bottom = max(row_bottom, node.y + node.height)

    def _normalize_origin(self) -> None:
        """Shift all nodes so the graph starts at the canvas margin."""
        if not self._nodes:
            return
        min_x = min(n.x for n in self._nodes)
        min_y = min(n.y for n in self._nodes)
        for node in self._nodes:
            node.x += _MARGIN - min_x
            node.y += _MARGIN - min_y

    def _resize_canvas(self) -> None:
        zoom = self._zoom()
        width = max((n.x + n.width for n in self._nodes), default=0) + _MARGIN
        height = max((n.y + n.height for n in self._nodes), default=0) + _MARGIN
        self._canvas.set_content_width(int(width * zoom))
        self._canvas.set_content_height(int(height * zoom))

    # Zoom

    def _zoom(self) -> float:
        return _ZOOM_STEPS[self._zoom_index]

    def _step_zoom(self, delta: int) -> None:
        index = min(max(self._zoom_index + delta, 0), len(_ZOOM_STEPS) - 1)
        if index == self._zoom_index:
            return
        self._zoom_index = index
        self._zoom_label.set_text(f"{round(self._zoom() * 100)}%")
        self._resize_canvas()
        self._canvas.queue_draw()

    # Dragging

    def _node_at(self, x: float, y: float) -> _Node | None:
        """Topmost node under widget coordinates."""
        zoom = self._zoom()
        cx, cy = x / zoom, y / zoom
        for node in reversed(self._nodes):
            if node.x <= cx <= node.x + node.width and (
                node.y <= cy <= node.y + node.height
            ):
                return node
        return None

    def _on_drag_begin(self, _gesture, x, y) -> None:
        node = self._node_at(x, y)
        self._drag_node = node
        if node is None:
            return
        self._drag_origin = (node.x, node.y)
        # Raise: draw (and hit-test) this node above the others.
        self._nodes.remove(node)
        self._nodes.append(node)
        self._canvas.queue_draw()

    def _on_drag_update(self, _gesture, dx, dy) -> None:
        node = self._drag_node
        if node is None:
            return
        zoom = self._zoom()
        node.x = max(0.0, self._drag_origin[0] + dx / zoom)
        node.y = max(0.0, self._drag_origin[1] + dy / zoom)
        self._canvas.queue_draw()

    def _on_drag_end(self, _gesture, _dx, _dy) -> None:
        if self._drag_node is not None:
            self._drag_node = None
            self._resize_canvas()

    def _on_motion(self, _controller, x, y) -> None:
        grabbing = self._drag_node is not None
        over = grabbing or self._node_at(x, y) is not None
        name = "grabbing" if grabbing else "grab" if over else "default"
        self._canvas.set_cursor(Gdk.Cursor.new_from_name(name))

    # Drawing

    def _draw(self, _area, cr, _width, _height) -> None:
        palette = _palette(Adw.StyleManager.get_default().get_dark())
        cr.scale(self._zoom(), self._zoom())
        nodes = {n.name: n for n in self._nodes}
        for rel in self._relations:
            source, target = nodes.get(rel.table), nodes.get(rel.ref_table)
            if source is not None and target is not None:
                self._draw_edge(cr, source, target, rel, palette)
        layout = self._pango_layout()
        for node in self._nodes:
            self._draw_node(cr, layout, node, palette)

    def _draw_edge(self, cr, source: _Node, target: _Node, rel, palette) -> None:
        y1 = source.row_anchor_y(rel.column)
        y2 = target.row_anchor_y(rel.ref_column)
        # Leave each node through the side facing the other one.
        if source.x + source.width / 2 <= target.x + target.width / 2:
            x1, x2 = source.x + source.width, target.x
            direction = 1
        else:
            x1, x2 = source.x, target.x + target.width
            direction = -1
        offset = max(24.0, min(72.0, abs(x2 - x1) / 2)) * direction
        cr.set_source_rgb(*_rgb(palette.edge))
        cr.set_line_width(1.2)
        cr.move_to(x1, y1)
        cr.curve_to(x1 + offset, y1, x2 - offset, y2, x2, y2)
        cr.stroke()
        # Arrowhead at the referenced column (the curve arrives
        # horizontally, so the tip points along `direction`).
        size = 6.0
        cr.move_to(x2, y2)
        cr.line_to(x2 - direction * size, y2 - size * 0.45)
        cr.line_to(x2 - direction * size, y2 + size * 0.45)
        cr.close_path()
        cr.fill()

    def _draw_node(self, cr, layout, node: _Node, palette) -> None:
        # Header
        cr.set_source_rgb(*_rgb(palette.header_bg))
        cr.rectangle(node.x, node.y, node.width, node.header_height)
        cr.fill()
        # Rows
        cr.set_source_rgb(*_rgb(palette.row_bg))
        cr.rectangle(
            node.x,
            node.y + node.header_height,
            node.width,
            node.height - node.header_height,
        )
        cr.fill()
        # Border and row separators
        cr.set_source_rgb(*_rgb(palette.border))
        cr.set_line_width(1.0)
        cr.rectangle(node.x + 0.5, node.y + 0.5, node.width - 1, node.height - 1)
        cr.stroke()
        for i in range(len(node.columns)):
            y = node.y + node.header_height + i * node.row_height
            cr.move_to(node.x, y + 0.5)
            cr.line_to(node.x + node.width, y + 0.5)
            cr.stroke()
        # Text
        cr.set_source_rgb(*_rgb(palette.fg))
        self._draw_text(
            cr, layout, node.name, node.x + _PAD_X, node.y + _PAD_Y, bold=True
        )
        for i, column in enumerate(node.columns):
            self._draw_text(
                cr,
                layout,
                _column_text(column),
                node.x + _PAD_X,
                node.y + node.header_height + i * node.row_height + _PAD_Y,
                bold=column.is_pk,
            )

    @staticmethod
    def _draw_text(cr, layout, text: str, x: float, y: float, bold: bool) -> None:
        layout.set_text(text, -1)
        attrs = Pango.AttrList()
        if bold:
            attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
        layout.set_attributes(attrs)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)

    def _show_message(self, title: str, description: str) -> None:
        self._placeholder.set_title(title)
        self._placeholder.set_description(description)
        self._stack.set_visible_child_name("message")


def _column_text(column: ColumnInfo) -> str:
    return f"{column.name}  {column.type}".rstrip()


def _dot_positions(
    nodes: list[_Node], relations: list[RelationInfo]
) -> dict[str, tuple[float, float]] | None:
    """Ask Graphviz to place fixed-size boxes; returns top-left corners
    in canvas pixels, or None when `dot` is unavailable or fails (the
    caller falls back to the grid layout)."""
    lines = [
        "digraph relations {",
        "  rankdir=LR;",
        "  nodesep=0.5; ranksep=0.9;",
        "  node [shape=box, fixedsize=true];",
    ]
    for node in nodes:
        lines.append(
            f"  {_quote(node.name)} "
            f"[width={node.width / 72:.4f}, height={node.height / 72:.4f}];"
        )
    for rel in relations:
        lines.append(f"  {_quote(rel.table)} -> {_quote(rel.ref_table)};")
    lines.append("}")
    try:
        completed = subprocess.run(
            ["dot", "-Tplain"],
            input="\n".join(lines).encode(),
            capture_output=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    positions: dict[str, tuple[float, float]] = {}
    graph_height = 0.0
    for line in completed.stdout.decode(errors="replace").splitlines():
        parts = shlex.split(line)
        if not parts:
            continue
        if parts[0] == "graph" and len(parts) >= 4:
            graph_height = float(parts[3])
        elif parts[0] == "node" and len(parts) >= 6:
            name = parts[1]
            # Plain output is in inches, y up, node center given.
            cx, cy = float(parts[2]) * 72, (graph_height - float(parts[3])) * 72
            w, h = float(parts[4]) * 72, float(parts[5]) * 72
            positions[name] = (cx - w / 2, cy - h / 2)
    return positions or None


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
