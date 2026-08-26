"""Accounts and permissions tab: who can reach this server, and what
each of them is allowed to do.

Two pages in an Adw.NavigationView. The first lists the accounts the
server reports (MySQL's 'name'@'host' pairs, PostgreSQL's roles —
login roles and the groups they belong to alike). Opening one pushes
the second, which is that account alone: its attributes, what it is
allowed to do (server-wide rights, per-database and per-table grants,
role memberships), and the buttons that act on it. The list is a way
in, not a column to keep looking at, so it steps aside once you are
reading one account.

Nothing here changes an account on its own. New User…, Set Password…,
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

from gi.repository import Adw, Gtk

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db.base import Connector, GrantScope, UserInfo
from sqlide.backend.workspaces import TabState
from sqlide.frontend.data_grid import ResultGrid
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
        self._seq = 0  # discards privilege loads for a since-closed page

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_margin_top(12)
        self._list.set_margin_bottom(12)
        self._list.set_margin_start(12)
        self._list.set_margin_end(12)
        self._list.connect("row-activated", self._row_activated)

        header = Adw.HeaderBar()
        new_user = Gtk.Button(label="New User…")
        new_user.connect("clicked", lambda *_: self._new_user())
        header.pack_start(new_user)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        describe(refresh, "Reload the account list")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_end(refresh)
        page = Adw.ToolbarView(
            content=Gtk.ScrolledWindow(child=self._list, vexpand=True)
        )
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
            return self._ensure(self.profile).list_users()

        def done(users: list[UserInfo]) -> None:
            self._seq += 1
            self._nav.pop_to_tag("list")
            self._users = users
            while (row := self._list.get_row_at_index(0)) is not None:
                self._list.remove(row)
            for user in users:
                self._list.append(_user_row(user))
            if not users:
                self._list.append(
                    Adw.ActionRow(title="No accounts reported")
                )

        run_async(work, done, lambda exc: self._show_error(str(exc)))

    def _row_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        index = row.get_index()
        if 0 <= index < len(self._users):
            self._open_user(self._users[index])

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
            ("Grant…", lambda: self._grant_dialog(user, revoke=False)),
            ("Revoke…", lambda: self._grant_dialog(user, revoke=True)),
            ("Set Password…", lambda: self._set_password(user)),
            ("Drop…", lambda: self._drop_user(user)),
        ):
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
        # Only MySQL makes the host part of the account; PostgreSQL
        # decides where a role may connect from in pg_hba.conf.
        if self.profile.kind == "mysql":
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


def _account_kind(user: UserInfo) -> str:
    return "login account" if user.can_login else "group role"


def _account_label(user: UserInfo) -> str:
    return f"{user.name}@{user.host}" if user.host else user.name


def _user_row(user: UserInfo) -> Adw.ActionRow:
    row = Adw.ActionRow(
        title=_account_label(user),
        subtitle=user.detail or _account_kind(user),
        activatable=True,
    )
    row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
    row.add_prefix(
        Gtk.Image(
            icon_name="avatar-default-symbolic"
            if user.can_login
            else "system-users-symbolic"
        )
    )
    return row


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
