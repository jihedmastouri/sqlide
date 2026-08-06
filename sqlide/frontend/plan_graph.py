"""Query plans drawn as a tree, the way the relation graph draws
schemas.

`EXPLAIN` answers in three different shapes, and none of them reads as
a plan in a table:

- SQLite's `EXPLAIN QUERY PLAN` returns (id, parent, notused, detail):
  an explicit tree, flattened into rows.
- PostgreSQL (and MySQL's `FORMAT=TREE`) return one text column whose
  indentation and `->` markers are the tree, with each node's
  attributes on the lines under it.
- MySQL's classic `EXPLAIN` returns one row per accessed table, in
  join order — a pipeline rather than a tree.

parse_plan() turns any of them into PlanNode roots (the last shape as
a chain, which is what it is), and anything it does not recognise into
one node per row, so the view always has something to draw. PlanGraph
is the widget: a cairo canvas with the root on top, children below it,
elbow connectors, zoom, and the node's full text on hover.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from gi.repository import Adw, Gtk, Pango

from sqlide.frontend.canvas import (
    FONT,
    Palette,
    draw_text,
    palette,
    rgb,
    rounded_rect,
    text_size,
)
from sqlide.frontend.util import describe

_ZOOM_STEPS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)

_PAD_X = 10  # text padding inside a node box
_PAD_Y = 6
_MARGIN = 24  # canvas margin around the tree
_H_GAP = 24  # space between sibling subtrees
_V_GAP = 34  # space between a node and its children
_MAX_CHARS = 46  # longest line kept in a box (the rest is in the tooltip)
_MAX_DETAILS = 3  # detail lines shown in a box


@dataclass
class PlanNode:
    """One step of a plan: a short title, the attributes under it, and
    the steps feeding it."""

    title: str
    details: list[str] = field(default_factory=list)
    children: list["PlanNode"] = field(default_factory=list)
    # Filled in by the layout, in canvas (unzoomed) coordinates.
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    def full_text(self) -> str:
        return "\n".join([self.title, *self.details])

    def walk(self) -> Iterable["PlanNode"]:
        yield self
        for child in self.children:
            yield from child.walk()


# Parsing


def parse_plan(columns: list[str], rows: list[tuple]) -> list[PlanNode]:
    """Plan rows as trees. Empty when there is nothing to draw."""
    if not rows:
        return []
    names = [c.lower() for c in columns]
    if names[:2] == ["id", "parent"] and "detail" in names:
        return _parse_sqlite(names, rows)
    if len(columns) == 1:
        return _parse_indented([_text(row[0]) for row in rows])
    return _parse_tabular(columns, rows)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _parse_sqlite(names: list[str], rows: list[tuple]) -> list[PlanNode]:
    """SQLite's (id, parent, notused, detail): a tree by id/parent,
    with 0 as the root's parent."""
    id_at, parent_at = names.index("id"), names.index("parent")
    detail_at = names.index("detail")
    nodes: dict[int, PlanNode] = {}
    order: list[tuple[int, int]] = []  # (id, parent), input order
    for row in rows:
        try:
            node_id = int(row[id_at])
            parent_id = int(row[parent_at])
        except (TypeError, ValueError):
            continue
        nodes[node_id] = _node_from_detail(_text(row[detail_at]))
        order.append((node_id, parent_id))
    roots = []
    for node_id, parent_id in order:
        parent = nodes.get(parent_id)
        if parent is not None and parent_id != node_id:
            parent.children.append(nodes[node_id])
        else:
            roots.append(nodes[node_id])
    return roots


def _node_from_detail(detail: str) -> PlanNode:
    """Split one SQLite plan line into a title and its qualifiers:
    "SEARCH orders USING INDEX ix (customer_id=?)" reads as a step with
    two attributes, not as one long line."""
    detail = detail.strip()
    for marker in (" USING ", " USE "):
        head, sep, tail = detail.partition(marker)
        if sep:
            return PlanNode(head.strip(), [(sep + tail).strip()])
    return PlanNode(detail)


def _parse_indented(lines: list[str]) -> list[PlanNode]:
    """PostgreSQL's text plan (and MySQL's FORMAT=TREE): `->` marks a
    node, its column is the depth, and the lines under it that are not
    nodes are its attributes."""
    roots: list[PlanNode] = []
    stack: list[tuple[int, PlanNode]] = []  # (indent of the marker, node)
    for raw in lines:
        for line in raw.splitlines():
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            stripped = line.strip()
            if stripped.startswith("->"):
                node = _node_from_plan_line(stripped[2:].strip())
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                if stack:
                    stack[-1][1].children.append(node)
                else:
                    roots.append(node)
                stack.append((indent, node))
            elif not stack and not roots:
                node = _node_from_plan_line(stripped)
                roots.append(node)
                stack.append((indent, node))
            elif stack:
                # An attribute of the innermost node still open at this
                # indentation (Filter:, Sort Key:, Buffers: …).
                while len(stack) > 1 and stack[-1][0] >= indent:
                    stack.pop()
                stack[-1][1].details.append(stripped)
            elif roots:
                roots[-1].details.append(stripped)
    return roots


def _node_from_plan_line(text: str) -> PlanNode:
    """"Seq Scan on users  (cost=0.00..1.04 rows=4 width=68)" → title
    plus the cost estimate as its own line."""
    head, sep, tail = text.partition("  (")
    if not sep:
        head, sep, tail = text.partition(" (cost")
        if sep:
            tail = "cost" + tail
    node = PlanNode(head.strip())
    if sep and tail.strip():
        node.details.append("(" + tail.strip().lstrip("("))
    return node


def _parse_tabular(columns: list[str], rows: list[tuple]) -> list[PlanNode]:
    """MySQL's classic EXPLAIN, and any other row-per-step plan: the
    rows are a pipeline in join order, so chain them — each row feeds
    the one above it. The title is the accessed object where the plan
    names one, the remaining non-empty columns are the attributes."""
    names = [c.lower() for c in columns]
    title_at = next(
        (names.index(c) for c in ("table", "detail", "operation") if c in names),
        None,
    )
    nodes = []
    for position, row in enumerate(rows, start=1):
        values = list(row) + [None] * (len(columns) - len(row))
        title = _text(values[title_at]) if title_at is not None else ""
        node = PlanNode(title or f"step {position}")
        node.details = [
            f"{column}: {_text(value)}"
            for index, (column, value) in enumerate(zip(columns, values))
            if index != title_at and _text(value) not in ("", "None")
        ]
        nodes.append(node)
    for upper, lower in zip(nodes, nodes[1:]):
        upper.children.append(lower)
    return nodes[:1]


# Layout


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_CHARS else text[: _MAX_CHARS - 1] + "…"


class PlanGraph(Gtk.Box):
    """The plan as a diagram: the step producing the final rows on top,
    what feeds it below, connected by elbows. Nodes are laid out by a
    tidy-tree pass — there is nothing to drag, so the picture is the
    same every time the same plan is drawn."""

    def __init__(self, roots: list[PlanNode]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._roots = roots
        self._zoom_index = _ZOOM_STEPS.index(1.0)

        bar = Gtk.Box(
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        zoom_box = Gtk.Box()
        zoom_box.add_css_class("linked")
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic")
        describe(zoom_out, "Zoom out")
        zoom_out.connect("clicked", lambda *_: self._step_zoom(-1))
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic")
        describe(zoom_in, "Zoom in")
        zoom_in.connect("clicked", lambda *_: self._step_zoom(1))
        zoom_box.append(zoom_out)
        zoom_box.append(zoom_in)
        self._zoom_label = Gtk.Label(label="100%")
        self._zoom_label.add_css_class("dim-label")
        hint = Gtk.Label(xalign=1, hexpand=True)
        hint.add_css_class("dim-label")
        hint.set_text(
            f"{sum(1 for root in roots for _ in root.walk())} step(s)"
            " · hover a step for its full text"
        )
        bar.append(zoom_box)
        bar.append(self._zoom_label)
        bar.append(hint)
        self.append(bar)

        self._canvas = Gtk.DrawingArea()
        self._canvas.set_draw_func(self._draw)
        self._canvas.set_has_tooltip(True)
        self._canvas.connect("query-tooltip", self._on_tooltip)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self._canvas)
        self.append(scroller)

        # The style can flip while the tab is open; the palette is read
        # at draw time, so a redraw is all it takes.
        style = Adw.StyleManager.get_default()
        handler = style.connect(
            "notify::dark", lambda *_: self._canvas.queue_draw()
        )
        self._canvas.connect("destroy", lambda *_: style.disconnect(handler))

        self._measure()
        self._layout()
        self._resize_canvas()

    # Geometry

    def _pango_layout(self) -> Pango.Layout:
        layout = self._canvas.create_pango_layout("")
        layout.set_font_description(Pango.FontDescription.from_string(FONT))
        return layout

    def _measure(self) -> None:
        layout = self._pango_layout()
        for node in self._nodes():
            width, height = text_size(layout, _clip(node.title), bold=True)
            total = height
            for detail in node.details[:_MAX_DETAILS]:
                w, h = text_size(layout, _clip(detail))
                width = max(width, w)
                total += h
            node.width = width + 2 * _PAD_X
            node.height = total + 2 * _PAD_Y

    def _layout(self) -> None:
        """Tidy tree, top down: a subtree is as wide as its children
        need, and every parent is centred over them."""
        x = float(_MARGIN)
        for root in self._roots:
            x += self._place(root, x, float(_MARGIN)) + _H_GAP

    def _place(self, node: PlanNode, x: float, y: float) -> float:
        """Place `node`'s subtree with its left edge at x, top at y;
        returns the width the subtree occupies."""
        if not node.children:
            node.x, node.y = x, y
            return node.width
        child_y = y + node.height + _V_GAP
        child_x = x
        widths = []
        for child in node.children:
            width = self._place(child, child_x, child_y)
            widths.append(width)
            child_x += width + _H_GAP
        span = sum(widths) + _H_GAP * (len(widths) - 1)
        if node.width > span:
            # The parent is wider than its children: re-centre them
            # under it rather than leaving them hanging off the left.
            self._shift(node.children, (node.width - span) / 2)
            span = node.width
        node.x = x + (span - node.width) / 2
        node.y = y
        return span

    def _shift(self, nodes: list[PlanNode], dx: float) -> None:
        for node in nodes:
            for descendant in node.walk():
                descendant.x += dx

    def _nodes(self) -> Iterable[PlanNode]:
        for root in self._roots:
            yield from root.walk()

    def _resize_canvas(self) -> None:
        zoom = self._zoom()
        width = max((n.x + n.width for n in self._nodes()), default=0) + _MARGIN
        height = max((n.y + n.height for n in self._nodes()), default=0) + _MARGIN
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

    # Drawing

    def _draw(self, _area, cr, _width, _height) -> None:
        colors = palette(Adw.StyleManager.get_default().get_dark())
        cr.scale(self._zoom(), self._zoom())
        layout = self._pango_layout()
        for node in self._nodes():
            for child in node.children:
                self._draw_edge(cr, node, child, colors)
        for node in self._nodes():
            self._draw_node(cr, layout, node, colors)

    def _draw_edge(
        self, cr, parent: PlanNode, child: PlanNode, colors: Palette
    ) -> None:
        x1 = parent.x + parent.width / 2
        y1 = parent.y + parent.height
        x2 = child.x + child.width / 2
        y2 = child.y
        middle = (y1 + y2) / 2
        cr.set_source_rgb(*rgb(colors.edge))
        cr.set_line_width(1.2)
        cr.move_to(x1, y1)
        cr.line_to(x1, middle)
        cr.line_to(x2, middle)
        cr.line_to(x2, y2)
        cr.stroke()
        # Arrowhead at the parent: rows flow from the child up into it.
        size = 5.0
        cr.move_to(x1, y1)
        cr.line_to(x1 - size * 0.6, y1 + size)
        cr.line_to(x1 + size * 0.6, y1 + size)
        cr.close_path()
        cr.fill()

    def _draw_node(self, cr, layout, node: PlanNode, colors: Palette) -> None:
        rounded_rect(cr, node.x, node.y, node.width, node.height, 6)
        cr.set_source_rgb(*rgb(colors.row_bg))
        cr.fill_preserve()
        cr.set_source_rgb(*rgb(colors.border))
        cr.set_line_width(1.0)
        cr.stroke()
        cr.set_source_rgb(*rgb(colors.fg))
        y = node.y + _PAD_Y
        draw_text(cr, layout, _clip(node.title), node.x + _PAD_X, y, bold=True)
        _ink, logical = layout.get_pixel_extents()
        y += logical.height
        for detail in node.details[:_MAX_DETAILS]:
            draw_text(cr, layout, _clip(detail), node.x + _PAD_X, y)
            _ink, logical = layout.get_pixel_extents()
            y += logical.height

    def _node_at(self, x: float, y: float) -> PlanNode | None:
        zoom = self._zoom()
        cx, cy = x / zoom, y / zoom
        for node in self._nodes():
            if node.x <= cx <= node.x + node.width and (
                node.y <= cy <= node.y + node.height
            ):
                return node
        return None

    def _on_tooltip(self, _area, x, y, keyboard, tooltip) -> bool:
        """Boxes hold a clipped title and the first few attributes;
        hovering shows the step whole."""
        if keyboard:
            return False
        node = self._node_at(x, y)
        if node is None:
            return False
        label = Gtk.Label(label=node.full_text(), xalign=0)
        label.add_css_class("monospace")
        tooltip.set_custom(label)
        return True


def plan_graph(columns: list[str], rows: list[tuple]) -> Gtk.Widget | None:
    """The graph for one explain result, or None when its rows carry no
    plan (the caller then shows only the table and JSON views)."""
    roots = parse_plan(columns, rows)
    if not roots:
        return None
    return PlanGraph(roots)
