"""Accounts and permissions tab: who can reach this server, and what
each of them is allowed to do.

Two pages in an Adw.NavigationView. The first is a table of the
accounts the server reports (MySQL's 'name'@'host' pairs, PostgreSQL's
roles — login roles and the groups they belong to alike), sortable by
any column and filtered by a search box. Which columns it has is the
provider's answer, not this file's: `principal_columns()` names the
attributes the engine actually records, so a host column appears for
MySQL and a "can create db" one for PostgreSQL without either engine
being mentioned here (CORE-12). Opening a row pushes
the second, which is that account alone: its attributes, what it is
allowed to do (server-wide rights, per-database and per-table grants,
role memberships), and the buttons that act on it. The list is a way
in, not a column to keep looking at, so it steps aside once you are
reading one account.

Permissions… is the exception to the paragraph below, and its own
screen: the permission editor (frontend/permission_editor.py) edits
grants object by object and runs its statements itself, after showing
every one of them in a Save dialog. It is offered only where the engine
declares the `permission_editor` capability.

Nothing else here changes an account on its own. New User…, Set Password…,
Grant…, Revoke… and Drop… each build the dialect's statement and open
it in a query console for the user to read and Run. Account changes
are the one kind of DDL whose blast radius isn't visible from the
statement — a revoked SELECT breaks whoever was relying on it — so
they take the same review path the sidebar's "New ▸" templates take,
rather than executing from a dialog.

Adapters that have no accounts (SQLite) or no portable catalog for
them (JDBC) leave Connector.supports_users False and never get here.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import Connector, GrantScope, UserInfo
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import ResultGrid
from sqlide.frontend.permission_editor import PermissionEditor
from sqlide.frontend.util import describe, run_async


class UsersTab(Gtk.Box):
    def __init__(
        self,
        profile: ConnectionProfile,
        ensure_connector: Callable[[ConnectionProfile], Connector],
        show_error: Callable[[str], None],
        on_open_sql: Callable[[ConnectionProfile, str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.profile = profile
        self._ensure = ensure_connector
        self._show_error = show_error
        self._on_open_sql = on_open_sql
        self._users: list[UserInfo] = []
        # A principal (and the object to open it on) asked for before
        # the account list arrived — a link out of an object's
        # Permissions section (CORE-11). Held until the list is in,
        # because reload() pops every page above the list.
        self._wanted: tuple[str, NodeRef | None] | None = None
        self._seq = 0  # discards privilege loads for a since-closed page

        # The table: a ListStore of rows, filtered by the search box,
        # sorted by whichever header was clicked, shown through a
        # ColumnView. Only visible rows are ever built into widgets, so
        # a server with hundreds of accounts scrolls like one with ten.
        self._store = Gio.ListStore(item_type=_PrincipalRow)
        self._search_text = ""
        self._filter = Gtk.CustomFilter.new(self._matches)
        filtered = Gtk.FilterListModel(model=self._store, filter=self._filter)
        self._view = Gtk.ColumnView(hexpand=True, vexpand=True)
        self._view.add_css_class("data-table")
        self._view.set_show_row_separators(True)
        sorted_model = Gtk.SortListModel(
            model=filtered, sorter=self._view.get_sorter()
        )
        selection = Gtk.SingleSelection(model=sorted_model, autoselect=False)
        self._view.set_model(selection)
        self._view.set_single_click_activate(True)
        self._view.connect("activate", self._row_activated)
        self._columns: tuple[str, ...] = ()
        self._empty = Adw.StatusPage(
            icon_name="system-users-symbolic",
            title="No accounts reported",
            description="This connection sees no accounts on the server.",
        )
        self._stack = Gtk.Stack()
        self._stack.add_named(
            Gtk.ScrolledWindow(child=self._view, vexpand=True), "table"
        )
        self._stack.add_named(self._empty, "empty")

        search = Gtk.SearchEntry(placeholder_text="Filter accounts")
        search.set_hexpand(True)
        search.connect("search-changed", self._search_changed)
        search_bar = Gtk.Box(
            margin_top=6, margin_bottom=6, margin_start=12, margin_end=12,
        )
        search_bar.append(search)

        header = Adw.HeaderBar()
        new_user = Gtk.Button(label="New User…")
        new_user.connect("clicked", lambda *_: self._new_user())
        header.pack_start(new_user)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, "Reload the account list")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_end(refresh)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(search_bar)
        body.append(self._stack)
        page = Adw.ToolbarView(content=body)
        page.add_top_bar(header)
        self._nav = Adw.NavigationView(vexpand=True)
        self._nav.add(
            Adw.NavigationPage(
                child=page, title=f"Users on {profile.name}", tag="list"
            )
        )
        self.append(self._nav)

        self.reload()

    def tab_state(self) -> TabState:
        return TabState(kind="users", connection=self.profile.name)

    # The account list

    def reload(self) -> None:
        """Refetch the accounts. Any account page open on top of the
        list is popped: the account it was showing may be gone, and a
        detail page nobody refreshed is worse than going back."""
        def work():
            provider = registry.create_provider(
                self.profile.kind, self._ensure(self.profile)
            )
            return provider.principal_table()

        def done(table) -> None:
            columns, rows = table
            self._seq += 1
            self._nav.pop_to_tag("list")
            self._users = [user for user, _cells in rows]
            self._set_columns(tuple(columns))
            self._store.remove_all()
            for index, (user, cells) in enumerate(rows):
                self._store.append(_PrincipalRow(index, user, tuple(cells)))
            self._stack.set_visible_child_name("table" if rows else "empty")
            self._open_wanted()

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _set_columns(self, columns: tuple[str, ...]) -> None:
        """Rebuild the header when the engine's column set changes —
        which is once, on the first load, unless the tab is pointed at
        another connection."""
        if columns == self._columns:
            return
        existing = self._view.get_columns()
        for column in [
            existing.get_item(i) for i in range(existing.get_n_items())
        ]:
            self._view.remove_column(column)
        self._columns = columns
        for index, name in enumerate(columns):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", _setup_cell)
            factory.connect("bind", _bind_cell, index)
            column = Gtk.ColumnViewColumn(title=name, factory=factory)
            column.set_resizable(True)
            column.set_expand(name in ("Name", "Member of"))
            column.set_sorter(
                Gtk.CustomSorter.new(_cell_sorter(index))
            )
            self._view.append_column(column)

    def _search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip().casefold()
        self._filter.changed(Gtk.FilterChange.DIFFERENT)

    def _matches(self, row: "_PrincipalRow") -> bool:
        """A row survives the filter when the search text appears in
        any of its cells — a name, a host, a role it is a member of are
        all things you would type looking for an account."""
        if not self._search_text:
            return True
        return any(self._search_text in cell.casefold() for cell in row.cells)

    def _row_activated(self, _view, position: int) -> None:
        model = self._view.get_model()
        row = model.get_item(position)
        if row is not None:
            self._open_user(row.user)

    # One account

    def _open_user(self, user: UserInfo) -> None:
        """Push the page for one account: its attributes, its
        privileges, and the buttons that act on it."""
        header = Adw.HeaderBar()
        actions = Gtk.Box(
            spacing=6,
            margin_top=6, margin_bottom=6, margin_start=12, margin_end=12,
        )
        subtitle = Gtk.Label(
            label=user.detail or _account_kind(user), xalign=0, hexpand=True
        )
        subtitle.add_css_class("dim-label")
        actions.append(subtitle)
        for label, callback in (
            ("Permissions…", lambda: self._open_permissions(user)),
            ("Grant…", lambda: self._grant_dialog(user, revoke=False)),
            ("Revoke…", lambda: self._grant_dialog(user, revoke=True)),
            ("Set Password…", lambda: self._set_password(user)),
            ("Drop…", lambda: self._drop_user(user)),
        ):
            if label == "Permissions…" and not registry.capabilities(
                self.profile.kind
            ).permission_editor:
                # An engine whose grants are not editable object by
                # object never offers the editor (db/metadata.py).
                continue
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, cb=callback: cb())
            if label == "Drop…":
                button.add_css_class("destructive-action")
            actions.append(button)

        grid = ResultGrid()
        grid.set_vexpand(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(actions)
        content.append(grid)
        view = Adw.ToolbarView(content=content)
        view.add_top_bar(header)
        self._nav.push(
            Adw.NavigationPage(child=view, title=_account_label(user))
        )

        self._seq += 1
        seq = self._seq

        def work():
            return self._ensure(self.profile).list_privileges(user)

        def done(privileges) -> None:
            if seq != self._seq:  # the page was left, or the list reloaded
                return
            grid.set_result(
                ["Scope", "Privilege", "Grantable"],
                [
                    (p.scope, p.privilege, "yes" if p.grantable else "")
                    for p in privileges
                ],
            )

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def open_permissions_for(
        self, principal: str, scope: NodeRef | None = None
    ) -> None:
        """Open the permission editor on one named principal, scoped to
        `scope` — how a row of an object's Permissions section arrives
        here (CORE-11).

        The name comes from a catalog grant, so it is matched against
        the accounts the server reports rather than trusted as one: a
        MySQL grantee reads 'app'@'%', a PostgreSQL grantee is a bare
        role name, and either may name an account that no longer
        exists.
        """
        self._wanted = (principal, scope)
        if self._users:
            self._open_wanted()

    def _open_wanted(self) -> None:
        wanted, self._wanted = self._wanted, None
        if wanted is None:
            return
        principal, scope = wanted
        user = _match_account(self._users, principal)
        if user is None:
            self._show_error(
                f"{principal} is not an account on this server any more"
            )
            return
        self._open_permissions(user, scope)

    def _open_permissions(
        self, user: UserInfo, scope: NodeRef | None = None
    ) -> None:
        """Push the permission editor for this account: the object tree
        on the left, what the account holds on the selected object on
        the right (CORE-10). It is the one screen here that runs its own
        statements — every change is reviewed in its Save dialog first,
        which is the same read-it-before-it-runs contract the other
        buttons keep by way of a console."""
        self._nav.push(
            Adw.NavigationPage(
                child=PermissionEditor(
                    self.profile,
                    user,
                    self._ensure,
                    self._show_error,
                    scope=scope,
                ),
                title=f"Permissions — {_account_label(user)}",
            )
        )

    # Actions — each builds a statement and opens it for review

    def _review(self, build: Callable[[Connector], str]) -> None:
        """Build a statement on a worker thread (adapters read the
        catalog to do it) and open it in a console for the user to
        run."""
        run_async(
            lambda: build(self._ensure(self.profile)),
            lambda sql: self._on_open_sql(self.profile, sql),
            lambda exc: self._show_error(str(exc)),
        )

    def _new_user(self) -> None:
        name = Adw.EntryRow(title="Name")
        host = Adw.EntryRow(title="Host", text="%")
        password = Adw.PasswordEntryRow(title="Password")
        group = Adw.PreferencesGroup()
        group.add(name)
        # Only some engines make the host part of the account (MySQL);
        # PostgreSQL decides where a role may connect from in
        # pg_hba.conf, so the field would mean nothing there.
        if registry.capabilities(self.profile.kind).account_hosts:
            group.add(host)
        group.add(password)

        def create() -> None:
            if not name.get_text().strip():
                self._show_error("An account needs a name")
                return
            self._review(
                lambda c: c.create_user_sql(
                    name.get_text().strip(),
                    host.get_text().strip(),
                    password.get_text(),
                )
            )

        _prompt(self, "New User", group, "Build Statement", create)

    def _set_password(self, user: UserInfo) -> None:
        password = Adw.PasswordEntryRow(title="New password")
        group = Adw.PreferencesGroup()
        group.add(password)

        def apply() -> None:
            if not password.get_text():
                self._show_error("Enter the new password")
                return
            self._review(
                lambda c: c.set_password_sql(user, password.get_text())
            )

        _prompt(
            self,
            f"Set password for {_account_label(user)}",
            group,
            "Build Statement",
            apply,
        )

    def _drop_user(self, user: UserInfo) -> None:
        self._review(lambda c: c.drop_user_sql(user))

    def _grant_dialog(self, user: UserInfo, revoke: bool) -> None:
        """Ask for a scope and a set of privileges, then build the
        GRANT/REVOKE. The scope list comes from the server, so it only
        offers databases and schemas that exist."""
        def work():
            connector = self._ensure(self.profile)
            return connector.grant_scopes(), connector.privilege_names()

        def ready(loaded) -> None:
            scopes, names = loaded
            self._present_grant(user, list(scopes), list(names), revoke)

        run_async(work, ready, lambda exc: self._show_error(str(exc)))

    def _present_grant(
        self,
        user: UserInfo,
        scopes: list[GrantScope],
        names: list[str],
        revoke: bool,
    ) -> None:
        if not scopes:
            self._show_error("This connection reports nothing to grant on")
            return
        verb = "Revoke" if revoke else "Grant"
        scope_row = Adw.ComboRow(
            title="On",
            model=Gtk.StringList.new([s.label for s in scopes]),
        )
        group = Adw.PreferencesGroup()
        group.add(scope_row)
        checks: list[tuple[str, Gtk.CheckButton]] = []
        privileges = Adw.PreferencesGroup(title="Privileges")
        box = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=3,
            margin_top=6, margin_bottom=6, margin_start=6, margin_end=6,
        )
        for privilege in names:
            check = Gtk.CheckButton(label=privilege)
            checks.append((privilege, check))
            box.append(check)
        privileges.add(box)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(group)
        content.append(
            Gtk.ScrolledWindow(
                child=privileges,
                height_request=260,
                propagate_natural_width=True,
            )
        )

        def build() -> None:
            chosen = [p for p, check in checks if check.get_active()]
            if not chosen:
                self._show_error("Select at least one privilege")
                return
            target = scopes[scope_row.get_selected()].target
            self._review(
                lambda c: (c.revoke_sql if revoke else c.grant_sql)(
                    user, chosen, target
                )
            )

        _prompt(
            self,
            f"{verb} privileges — {_account_label(user)}",
            content,
            "Build Statement",
            build,
        )


def _match_account(users: list[UserInfo], principal: str) -> UserInfo | None:
    """The account a grant's grantee names, or None.

    A grantee is spelled the way its own dialect spells an account, so
    "'app'@'%'", "app@%" and "app" all have to find the same row.
    """
    wanted = principal.strip().replace("'", "")
    for user in users:
        if wanted in (_account_label(user), user.name):
            return user
    name, _, host = wanted.partition("@")
    for user in users:
        if user.name == name and (not host or host in ("%", user.host)):
            return user
    return None


def _account_kind(user: UserInfo) -> str:
    return "login account" if user.can_login else "group role"


def _account_label(user: UserInfo) -> str:
    return f"{user.name}@{user.host}" if user.host else user.name


class _PrincipalRow(GObject.Object):
    """One row of the overview: the account, and the cells the provider
    rendered for it. The account rides along so activating a row opens
    that principal without the table being parsed back into a name."""

    def __init__(
        self, index: int, user: UserInfo, cells: tuple[str, ...]
    ) -> None:
        super().__init__()
        self.index = index
        self.user = user
        self.cells = cells


def _cell(row: _PrincipalRow, index: int) -> str:
    return row.cells[index] if index < len(row.cells) else ""


def _cell_sorter(index: int) -> Callable[[object, object], int]:
    """Sort one column as text, case-insensitively, with empty cells
    after filled ones — ascending on "Superuser" is then the superusers
    first rather than a wall of blanks. Equal cells keep the order the
    server listed them in, so a sort never shuffles ties.""" 
    def compare(left, right, *_args) -> int:
        a, b = _cell(left, index).casefold(), _cell(right, index).casefold()
        if (not a) != (not b):
            return 1 if not a else -1
        if a == b:
            return (left.index > right.index) - (left.index < right.index)
        return 1 if a > b else -1

    return compare


def _setup_cell(_factory, item: Gtk.ListItem) -> None:
    label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
    label.set_margin_start(6)
    label.set_margin_end(6)
    item.set_child(label)


def _bind_cell(_factory, item: Gtk.ListItem, index: int) -> None:
    item.get_child().set_text(_cell(item.get_item(), index))


def _prompt(
    parent: Gtk.Widget,
    heading: str,
    child: Gtk.Widget,
    confirm_label: str,
    on_confirm: Callable[[], None],
) -> None:
    """A form dialog whose confirm response only ever builds SQL — the
    statement itself is reviewed and run in a console afterwards, so
    the default response here is safe to be the confirming one."""
    dialog = Adw.AlertDialog(heading=heading, extra_child=child)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", confirm_label)
    dialog.set_response_appearance(
        "confirm", Adw.ResponseAppearance.SUGGESTED
    )
    dialog.set_default_response("confirm")
    dialog.set_close_response("cancel")
    dialog.connect(
        "response",
        lambda _d, response: on_confirm() if response == "confirm" else None,
    )
    dialog.present(parent)
