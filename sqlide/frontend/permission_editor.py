"""The permission editor: one principal, the object tree, checkboxes.

Reached from the users tab, on top of one account (frontend/users_tab.py):
the principal is chosen first and the whole screen is scoped to it, so
every checkbox on it reads "this account, on this object".

Split screen. On the left the same object hierarchy the sidebar walks —
built from the metadata provider, so it is the engine's own shape
(connection → database → schema → object on PostgreSQL, one level
shorter on MySQL) rather than a tree this module knows how to draw. On
the right the privileges that principal holds on whatever is selected,
one row per privilege the engine allows *there*: a schema offers USAGE
and CREATE, a table its seven, a MySQL column the four that can name
one. The provider decides all of it (db/metadata.py `privileges_for`,
`grant_target`), so nothing here branches on the engine.

Three rules the checkboxes keep:

* A privilege the account holds through a role it belongs to is shown
  with the role that carries it and cannot be ticked. Revoking it here
  would either fail or take it from everyone else in that role.
* "Grant option" is a checkbox of its own next to the privilege, so
  WITH GRANT OPTION is something you can see and set, not a mode.
* Nothing is written as you click. Changes pile up across objects —
  the tree marks the ones you touched — until **Save**, which shows
  every statement it is about to run, grouped by object, and runs them
  in one transaction where the engine has one (PostgreSQL). **Revert**
  throws the pile away.

A row of an object's Permissions section (CORE-11) opens this screen
too, on the principal that row names and already scoped to the object
it was read from — the same editor, entered from the other end.

The tree is scoped to what can actually be granted on (CORE-54): the
provider answers `grantable_subtree` for every node, so a folder of
indexes, a trigger, or a table on an engine with no column grants is
left out rather than scrolled past, and a folder whose whole subtree is
ungrantable is hidden instead of shown empty. A header above the split
view names the principal and stays there while the tree scrolls.

Engines with no privilege system never get here: SQLite leaves the
`permission_editor` capability off and the users tab hides the button.
Reached anyway, the screen says the engine grants nothing rather than
showing a tree with nothing in it.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gio, GObject, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector, UserInfo
from sqlide.backend.db.metadata import (
    MetadataProvider,
    NodeRef,
    PermissionSet,
)
from sqlide.frontend.util import describe, run_async

#: Node kinds worth expanding. A leaf that has children the editor
#: cannot grant on (an index's columns) is not one of them.
_EXPANDABLE = ("connection", "database", "schema", "category", "table", "view")


class _Node(GObject.Object):
    """One row of the object tree, wrapping the provider's NodeRef."""

    def __init__(self, ref: NodeRef) -> None:
        super().__init__()
        self.ref = ref
        self.store: Gio.ListStore | None = None
        self.loaded = False

    @property
    def key(self) -> str:
        ref = self.ref
        return "|".join(
            (ref.kind, ref.database, ref.schema, ref.table, ref.name)
        )

    @property
    def label(self) -> str:
        return self.ref.name or self.ref.kind


class PermissionEditor(Gtk.Box):
    """The split screen for one principal."""

    def __init__(
        self,
        profile: ConnectionProfile,
        user: UserInfo,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        *,
        scope: NodeRef | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self.user = user
        self._ensure = ensure_connector
        self._show_error = show_error
        # The object to open on, when the editor was reached from that
        # object's Permissions section (CORE-11) instead of from the
        # account list: the right-hand grid starts on it rather than on
        # "Pick an object".
        self._scope = scope
        self._provider: MetadataProvider | None = None
        # Pending edits, per object: the set that was loaded, and the
        # privileges the user has moved since. Kept by node key so a
        # tree row can ask "did I change?" without a lookup by object.
        self._pending: dict[str, tuple[_Node, PermissionSet, dict]] = {}
        self._current: _Node | None = None
        # The "changed" dot of every row currently on screen, by node
        # key: a pending edit has to light the row it happened on
        # without rebuilding the tree under the selection.
        self._markers: dict[str, Gtk.Widget] = {}
        self._seq = 0  # discards a load whose row was left behind

        self._roots = Gio.ListStore(item_type=_Node)
        self._tree = Gtk.TreeListModel.new(
            self._roots,
            passthrough=False,
            autoexpand=False,
            create_func=self._children_of,
        )
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)
        factory.connect("unbind", self._unbind_row)
        selection = Gtk.SingleSelection(model=self._tree, autoselect=False)
        selection.connect("selection-changed", self._selection_changed)
        self._selection = selection
        self._list = Gtk.ListView(model=selection, factory=factory)
        self._list.add_css_class("navigation-sidebar")

        self._status = Adw.StatusPage(
            icon_name="dialog-password-symbolic",
            title="Pick an object",
            description=(
                "The privileges "
                f"{_label(user)} holds on it appear here."
            ),
        )
        self._right = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._right.set_child(self._status)

        paned = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            position=280,
            shrink_start_child=False,
            resize_start_child=False,
            vexpand=True,
        )
        paned.set_start_child(
            Gtk.ScrolledWindow(child=self._list, width_request=220)
        )
        paned.set_end_child(self._right)

        header = Adw.HeaderBar()
        self._save = Gtk.Button(label="Save…")
        self._save.add_css_class("suggested-action")
        describe(self._save, "Review and run the pending permission changes")
        self._save.connect("clicked", lambda *_: self._review())
        self._revert = Gtk.Button(label="Revert")
        describe(self._revert, "Discard every pending change")
        self._revert.connect("clicked", lambda *_: self._revert_all())
        header.pack_end(self._save)
        header.pack_end(self._revert)
        self._pending_label = Gtk.Label(xalign=0)
        self._pending_label.add_css_class("dim-label")
        header.pack_start(self._pending_label)

        self._view = Adw.ToolbarView(content=paned)
        self._view.add_top_bar(header)
        self._view.add_top_bar(_subject_bar(user))
        self.append(self._view)
        self._update_pending_state()
        self._load_root()

    # The object tree

    def _load_root(self) -> None:
        def work():
            connector = self._ensure(self.profile)
            provider = registry.create_provider(self.profile.kind, connector)
            root = provider.root(self.profile.name)
            return provider, root, provider.grantable_children(root)

        def done(loaded) -> None:
            provider, root, children = loaded
            self._provider = provider
            if not provider.grantable_kinds():
                self._no_grants()
                return
            self._roots.remove_all()
            self._roots.append(_Node(root))
            node = self._roots.get_item(0)
            node.store = Gio.ListStore(item_type=_Node)
            for child in children:
                node.store.append(_Node(child))
            node.loaded = True
            self._list.get_model().set_selected(0)
            self._show(_Node(self._scope) if self._scope else node)

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _no_grants(self) -> None:
        """The engine has no privilege system: say so where the tree
        would have been, rather than leaving an empty one (CORE-54)."""
        self._save.set_visible(False)
        self._revert.set_visible(False)
        self._view.set_content(
            Adw.StatusPage(
                icon_name="dialog-information-symbolic",
                title="No permissions to edit",
                description=(
                    f"{self.profile.name} runs an engine with no "
                    "privilege system: it grants nothing to anyone, so "
                    f"there is nothing here to set for {_label(self.user)}."
                ),
            )
        )

    def _children_of(self, node: _Node) -> Gio.ListStore | None:
        # Called on expansion and by is-expandable probes, so it does no
        # I/O of its own: it hands back the store and fills it after.
        if node.ref.kind not in _EXPANDABLE:
            return None
        if self._provider is not None and not self._expandable(node.ref):
            return None
        if node.store is None:
            node.store = Gio.ListStore(item_type=_Node)
            self._fill(node)
        return node.store

    def _fill(self, node: _Node) -> None:
        if node.loaded or self._provider is None:
            return
        node.loaded = True
        provider = self._provider

        def work():
            return provider.grantable_children(node.ref)

        def done(children) -> None:
            if node.store is None:
                return
            node.store.remove_all()
            for child in children:
                node.store.append(_Node(child))

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _expandable(self, ref: NodeRef) -> bool:
        """Whether the tree should offer to open `ref`. A table is only
        worth opening where the engine grants on columns."""
        provider = self._provider
        if provider is None:
            return False
        if ref.kind in ("table", "view"):
            return "column" in provider.grantable_kinds()
        return provider.grantable_subtree(ref)

    def _setup_row(self, _factory, item: Gtk.ListItem) -> None:
        expander = Gtk.TreeExpander()
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Label(xalign=0))
        marker = Gtk.Label(label="●")
        marker.add_css_class("accent")
        marker.set_visible(False)
        box.append(marker)
        expander.set_child(box)
        item.set_child(expander)

    def _bind_row(self, _factory, item: Gtk.ListItem) -> None:
        row = item.get_item()
        node = row.get_item()
        expander = item.get_child()
        expander.set_list_row(row)
        box = expander.get_child()
        label = box.get_first_child()
        label.set_label(node.label)
        marker = label.get_next_sibling()
        marker.set_visible(node.key in self._pending)
        describe(
            marker, "This object has permission changes waiting to be saved"
        )
        self._markers[node.key] = marker

    def _unbind_row(self, _factory, item: Gtk.ListItem) -> None:
        row = item.get_item()
        if row is not None:
            self._markers.pop(row.get_item().key, None)

    def _selection_changed(self, selection, *_args) -> None:
        row = selection.get_selected_item()
        if row is not None:
            self._show(row.get_item())

    # The privilege grid

    def _show(self, node: _Node) -> None:
        """Load and draw what the principal holds on `node`."""
        self._current = node
        if self._provider is None:
            return
        provider = self._provider
        self._seq += 1
        seq = self._seq
        user = self.user

        pending = self._pending.get(node.key)
        if pending is not None:
            self._draw(node, pending[1], pending[2])
            return

        def work():
            return provider.permission_set(user, node.ref)

        def done(permissions: PermissionSet) -> None:
            if seq != self._seq:
                return
            self._draw(node, permissions, {})

        self._right.set_child(
            Adw.StatusPage(
                title="Loading…", icon_name="content-loading-symbolic"
            )
        )
        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _draw(
        self, node: _Node, permissions: PermissionSet, edits: dict
    ) -> None:
        if not permissions.entries:
            self._right.set_child(
                Adw.StatusPage(
                    icon_name="dialog-information-symbolic",
                    title="Nothing to grant here",
                    description=(
                        f"{_object_label(node.ref)} carries no privileges of "
                        "its own on this engine."
                    ),
                )
            )
            return
        group = Adw.PreferencesGroup(
            title=_object_label(node.ref),
            description=f"What {_label(self.user)} may do here. "
            f"Granted on: {permissions.target}",
        )
        for entry in permissions.entries:
            granted, grantable = edits.get(
                entry.privilege, (entry.granted, entry.grantable)
            )
            row = Adw.ActionRow(title=entry.privilege)
            if entry.inherited_from:
                row.set_subtitle(f"via role {entry.inherited_from}")
                row.set_sensitive(False)
            elif entry.granted != granted or entry.grantable != grantable:
                row.set_subtitle("changed — not saved yet")
            grant_option = Gtk.CheckButton(
                active=grantable,
                sensitive=granted and entry.editable,
                valign=Gtk.Align.CENTER,
            )
            describe(grant_option, "May pass this privilege on to others")
            option_label = Gtk.Label(
                label="grant option", valign=Gtk.Align.CENTER
            )
            option_label.add_css_class("dim-label")
            check = Gtk.CheckButton(active=granted, valign=Gtk.Align.CENTER)
            check.set_sensitive(entry.editable)
            describe(check, f"Grant {entry.privilege}")
            row.add_suffix(option_label)
            row.add_suffix(grant_option)
            row.add_suffix(check)
            row.set_activatable_widget(check if entry.editable else None)

            def changed(_widget, privilege=entry.privilege,
                        box=check, option=grant_option) -> None:
                on = box.get_active()
                option.set_sensitive(on)
                if not on:
                    option.set_active(False)
                self._edit(node, permissions, privilege,
                           on, option.get_active())

            check.connect("toggled", changed)
            grant_option.connect("toggled", changed)
            group.add(row)
        page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )
        page.append(group)
        self._right.set_child(page)

    def _edit(
        self,
        node: _Node,
        permissions: PermissionSet,
        privilege: str,
        granted: bool,
        grantable: bool,
    ) -> None:
        """Record one checkbox move. An edit that puts a privilege back
        where the server has it stops being a change, and an object
        with no changes left drops out of the pending list — otherwise
        the tree would keep marking a row nobody is changing."""
        _node, loaded, edits = self._pending.get(
            node.key, (node, permissions, {})
        )
        state = loaded.state(privilege)
        if state is not None and (
            state.granted, state.grantable
        ) == (granted, grantable):
            edits.pop(privilege, None)
        else:
            edits[privilege] = (granted, grantable)
        if edits:
            self._pending[node.key] = (node, loaded, edits)
        else:
            self._pending.pop(node.key, None)
        self._update_pending_state()

    def _update_pending_state(self) -> None:
        objects = len(self._pending)
        changes = sum(len(edits) for _n, _s, edits in self._pending.values())
        self._save.set_sensitive(bool(objects))
        self._revert.set_sensitive(bool(objects))
        self._pending_label.set_label(
            ""
            if not objects
            else f"{changes} pending change{'' if changes == 1 else 's'} "
            f"on {objects} object{'' if objects == 1 else 's'}"
        )
        for key, marker in self._markers.items():
            marker.set_visible(key in self._pending)

    def _revert_all(self) -> None:
        self._pending.clear()
        self._update_pending_state()
        if self._current is not None:
            self._show(self._current)

    # Saving

    def _review(self) -> None:
        """Build every pending statement, then show them grouped by
        object before anything runs."""
        if self._provider is None or not self._pending:
            return
        provider = self._provider
        user = self.user
        pending = list(self._pending.values())

        def work():
            groups = []
            for node, permissions, edits in pending:
                statements = provider.permission_statements(
                    user, permissions, edits
                )
                if statements:
                    groups.append((_object_label(node.ref), statements))
            return groups

        def done(groups) -> None:
            if not groups:
                self._show_error("Nothing left to change")
                return
            self._confirm(groups)

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _confirm(self, groups: list[tuple[str, list[str]]]) -> None:
        statements = [sql for _label_, group in groups for sql in group]
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for label, group in groups:
            heading = Gtk.Label(label=label, xalign=0)
            heading.add_css_class("heading")
            body.append(heading)
            text = Gtk.Label(
                label=";\n".join(group) + ";",
                xalign=0, wrap=True, selectable=True,
            )
            text.add_css_class("monospace")
            body.append(text)
        transactional = self._provider.capabilities().transactional_grants
        dialog = Adw.AlertDialog(
            heading=f"Apply {len(statements)} statement"
            f"{'' if len(statements) == 1 else 's'}?",
            body=(
                f"These run on {self.profile.name} as written"
                + (
                    ", in one transaction: a failure leaves nothing "
                    "applied."
                    if transactional
                    else ". This engine commits each statement as it "
                    "runs, so a failure stops at that statement and "
                    "leaves the ones before it in place."
                )
            ),
            extra_child=Gtk.ScrolledWindow(
                child=body,
                height_request=280,
                propagate_natural_width=True,
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("run", "Run")
        dialog.set_response_appearance(
            "run", Adw.ResponseAppearance.DESTRUCTIVE
        )
        # Cancel by default: a permission change is not one Enter away.
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, response: (
                self._apply(statements) if response == "run" else None
            ),
        )
        dialog.present(self)

    def _apply(self, statements: list[str]) -> None:
        provider = self._provider
        if provider is None:
            return

        def work():
            provider.apply_permissions(statements)
            return True

        def done(_ok) -> None:
            self._pending.clear()
            self._update_pending_state()
            if self._current is not None:
                self._show(self._current)

        run_async(work, done, lambda exc: self._show_error(str(exc)))


def _subject_bar(user: UserInfo) -> Gtk.Widget:
    """The header naming whose permissions these are, pinned above the
    split view so the subject stays on screen while the tree scrolls
    (CORE-54)."""
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        margin_top=6, margin_bottom=6, margin_start=12, margin_end=12,
    )
    title = Gtk.Label(label=_label(user), xalign=0)
    title.add_css_class("title-4")
    subtitle = Gtk.Label(label=_subject_detail(user), xalign=0)
    subtitle.add_css_class("dim-label")
    subtitle.add_css_class("caption")
    box.append(title)
    box.append(subtitle)
    describe(box, f"Editing the permissions of {_label(user)}")
    box.add_css_class("toolbar")
    return box


def _subject_detail(user: UserInfo) -> str:
    """The line under the name: what kind of principal it is, and
    whether it can log in."""
    kind = (user.kind or "user").capitalize()
    return f"{kind} · {'can log in' if user.can_login else 'no login'}"


def _label(user: UserInfo) -> str:
    return f"{user.name}@{user.host}" if user.host else user.name


def _object_label(ref: NodeRef) -> str:
    """The object as the editor names it: the kind plus the path that
    tells two same-named objects apart."""
    where = ".".join(part for part in (ref.schema or ref.database,) if part)
    if ref.kind == "connection":
        return "Server"
    if ref.kind == "column" and ref.table:
        return f"Column {where}.{ref.table}.{ref.name}" if where else (
            f"Column {ref.table}.{ref.name}"
        )
    name = f"{where}.{ref.name}" if where and ref.kind not in (
        "database", "schema"
    ) else ref.name
    return f"{ref.kind.capitalize()} {name}"
