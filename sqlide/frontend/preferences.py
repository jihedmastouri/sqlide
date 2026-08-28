"""Preferences dialog and about dialog.

Both are opened through the app.preferences / app.about actions (menu
button in the main window and launcher headers). Every row writes to
the settings store on change — there is no Apply step — and the
application, subscribed to the store, restyles live (theme, editor
font). LSP defaults only affect servers started after the change;
already-running servers keep their session.
"""

from __future__ import annotations

from gi.repository import Adw, Gdk, GObject, Gtk

from sqlide import APP_ID, __version__, i18n
from sqlide.backend.settings import (
    KEYWORD_CASES,
    THEMES,
    TIME_ZONES,
    Settings,
    store,
)
from sqlide.backend.sql_risk import CONFIRM_MODES
from sqlide.frontend import feedback, keymap
from sqlide.frontend.backup_dialog import BackupWindow
from sqlide.i18n import _, N_
from sqlide.lsp import servers as lsp_servers

# Connection kinds that get a default-LSP row, with display titles.
_LSP_KINDS = (
    ("sqlite", "SQLite"),
    ("mysql", "MySQL"),
    ("postgres", "PostgreSQL"),
    ("jdbc", "JDBC"),
)
# These tables are built at import time, before install() has bound
# the catalogue, so they are marked with N_ and translated where they
# are read (see _labels).
_THEME_LABELS = (N_("Follow System"), N_("Light"), N_("Dark"))
# Parallel to sql_risk.CONFIRM_MODES.
_CONFIRM_LABELS = (
    N_("Always"),
    N_("Outside Development"),
    N_("Never"),
)
# Parallel to settings.KEYWORD_CASES.
_KEYWORD_CASE_LABELS = (
    N_("UPPER CASE"),
    N_("lower case"),
    N_("Follow What You Type"),
)
# Parallel to settings.TIME_ZONES.
_TIME_ZONE_LABELS = (
    N_("This Computer"),
    N_("UTC"),
    N_("Server Default"),
)


def _labels(labels: tuple[str, ...]) -> Gtk.StringList:
    """A combo row's model, with every label translated now — the
    strings themselves were marked with N_ at import time."""
    return Gtk.StringList.new([_(label) for label in labels])


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self) -> None:
        super().__init__()
        self.add(self._general_page(store.settings))
        self.add(self._shortcuts_page())
        self.add(self._lsp_page(store.settings))

    def _on_language_selected(self, row, *_args) -> None:
        code = self._language_codes[row.get_selected()]
        if code == store.settings.language:
            return
        store.update(language=code)
        feedback.toast(self, _("Language applies when sqlide restarts"))

    # General: appearance

    def _general_page(self, settings: Settings) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title=_("General"), icon_name="preferences-system-symbolic"
        )
        group = Adw.PreferencesGroup(title=_("Appearance"))

        # Only the languages with a compiled catalogue behind them —
        # offering one we do not ship would be a promise the app
        # cannot keep. "System" first, then each language named in
        # itself. The catalogue binds at startup, before the first
        # widget exists, so a change here asks for a restart rather
        # than retranslating the running UI.
        self._language_codes = [i18n.SYSTEM, *i18n.available_languages()]
        language_row = Adw.ComboRow(
            title=_("Interface Language"),
            subtitle=_(
                "Follow the system, or pick one of the shipped "
                "translations. Takes effect at the next start."
            ),
            model=Gtk.StringList.new(
                [_("System"), *i18n.available_languages().values()]
            ),
        )
        if settings.language in self._language_codes:
            language_row.set_selected(
                self._language_codes.index(settings.language)
            )
        language_row.connect("notify::selected", self._on_language_selected)
        group.add(language_row)

        theme_row = Adw.ComboRow(
            title=_("Theme"), model=_labels(_THEME_LABELS)
        )
        theme_row.set_selected(THEMES.index(settings.theme))
        theme_row.connect(
            "notify::selected",
            lambda row, *_: store.update(theme=THEMES[row.get_selected()]),
        )
        group.add(theme_row)

        font_row = Adw.SpinRow.new_with_range(6, 32, 1)
        font_row.set_title(_("Editor Font Size"))
        font_row.set_subtitle(_("In points, for the SQL editor"))
        font_row.set_value(settings.editor_font_size)
        font_row.connect(
            "notify::value",
            lambda row, *_: store.update(
                editor_font_size=int(row.get_value())
            ),
        )
        group.add(font_row)

        vim_row = Adw.SwitchRow(
            title=_("Vim Mode"),
            subtitle=_("Modal Vim editing in SQL editors "
            "(needs GtkSourceView)"),
        )
        vim_row.set_active(settings.vim_mode)
        vim_row.connect(
            "notify::active",
            lambda row, *_: store.update(vim_mode=row.get_active()),
        )
        group.add(vim_row)

        system_row = Adw.SwitchRow(
            title=_("Show System Schemas"),
            subtitle=_("Keep information_schema and the server's own "
            "catalog in the object tree, dimmed and last"),
        )
        system_row.set_active(settings.show_system_schemas)
        system_row.connect(
            "notify::active",
            lambda row, *_: store.update(
                show_system_schemas=row.get_active()
            ),
        )
        group.add(system_row)

        follow_row = Adw.SwitchRow(
            title=_("Follow the Active Tab"),
            subtitle=_("Highlight the object the current tab is showing "
            "in the tree, expanding the rows on the way to it"),
        )
        follow_row.set_active(settings.sidebar_follow_active_tab)
        follow_row.connect(
            "notify::active",
            lambda row, *_: store.update(
                sidebar_follow_active_tab=row.get_active()
            ),
        )
        group.add(follow_row)

        case_row = Adw.ComboRow(
            title=_("Keyword Completion Case"),
            subtitle=_("How completion spells a keyword. Table and "
            "column names always keep the case the database reports."),
            model=_labels(_KEYWORD_CASE_LABELS),
        )
        case_row.set_selected(KEYWORD_CASES.index(settings.sql_keyword_case))
        case_row.connect(
            "notify::selected",
            lambda row, *_: store.update(
                sql_keyword_case=KEYWORD_CASES[row.get_selected()]
            ),
        )
        group.add(case_row)

        indent_row = Adw.SpinRow.new_with_range(1, 8, 1)
        indent_row.set_title(_("Format Indent"))
        indent_row.set_subtitle(_("Spaces per indent step when Format "
                                "lays a statement out. Keywords follow "
                                "the case chosen above."))
        indent_row.set_value(settings.sql_format_indent)
        indent_row.connect(
            "notify::value",
            lambda row, *_: store.update(
                sql_format_indent=int(row.get_value())
            ),
        )
        group.add(indent_row)

        comma_row = Adw.SwitchRow(
            title=_("Leading Commas"),
            subtitle=_("Format starts a list item's line with its comma "
            "(\", name\") instead of ending the line before it"),
        )
        comma_row.set_active(settings.sql_format_comma_leading)
        comma_row.connect(
            "notify::active",
            lambda row, *_: store.update(
                sql_format_comma_leading=row.get_active()
            ),
        )
        group.add(comma_row)

        page.add(group)

        results = Adw.PreferencesGroup(
            title=_("Results"),
            description="A statement that returns more rows than the "
            "cap is fetched only up to it, and the result is marked as "
            "truncated. Without a cap, one SELECT over a large table "
            "pulls the whole thing into memory.",
        )
        rows_row = Adw.SpinRow.new_with_range(0, 1_000_000, 500)
        rows_row.set_title(_("Maximum Rows Fetched"))
        rows_row.set_subtitle(_("Per statement in a console or query "
                              "builder. 0 fetches everything."))
        rows_row.set_value(settings.max_result_rows)
        rows_row.connect(
            "notify::value",
            lambda row, *_: store.update(max_result_rows=int(row.get_value())),
        )
        results.add(rows_row)

        zone_row = Adw.ComboRow(
            title=_("Session Time Zone"),
            subtitle=_("What a new connection asks the server to report "
            "timestamps in. Takes effect on the next connect."),
            model=_labels(_TIME_ZONE_LABELS),
        )
        zone_row.set_selected(TIME_ZONES.index(settings.time_zone))
        zone_row.connect(
            "notify::selected",
            lambda row, *_: store.update(
                time_zone=TIME_ZONES[row.get_selected()]
            ),
        )
        results.add(zone_row)
        page.add(results)

        safety = Adw.PreferencesGroup(
            title=_("Safety"),
            description="How much friction a DROP, TRUNCATE, DELETE or "
            "UPDATE gets before it runs. Production connections always "
            "ask for the object's name on the destructive ones.",
        )
        confirm_row = Adw.ComboRow(
            title=_("Confirm Destructive Statements"),
            model=_labels(_CONFIRM_LABELS),
        )
        confirm_row.set_selected(
            CONFIRM_MODES.index(settings.confirm_destructive)
        )
        confirm_row.connect(
            "notify::selected",
            lambda row, *_: store.update(
                confirm_destructive=CONFIRM_MODES[row.get_selected()]
            ),
        )
        safety.add(confirm_row)
        page.add(safety)
        page.add(self._map_group(settings))

        backup_group = Adw.PreferencesGroup(
            title=_("Backup"),
            description="Settings and workspaces only. Database backups —"
            " scheduled dumps to disk, S3, SFTP or FTP — live in the "
            "Backups tab.",
        )
        backup_row = Adw.ActionRow(
            title=_("Backup &amp; Restore…"),
            subtitle=_("Export or restore settings and workspaces"),
            activatable=True,
        )
        backup_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        backup_row.connect("activated", self._open_backup_window)
        backup_group.add(backup_row)
        page.add(backup_group)
        page.add(self._transfer_group())
        return page

    # Workspace transfer (XML export/import)

    def _map_group(self, settings: Settings) -> Adw.PreferencesGroup:
        """The geo viewer's tile source (PG-04).

        Configurable because the default is somebody else's donated
        bandwidth: OpenStreetMap's tile policy asks that heavy users
        run or pay for their own server, and a machine with no network
        should be able to turn tiles off outright rather than wait for
        them. The credit line is a setting because it belongs to the
        server the tiles come from — blanking it turns tiles off
        instead of drawing them uncredited.
        """
        group = Adw.PreferencesGroup(
            title=_("Map"),
            description="How the geo viewer draws geometry columns. "
            "Tiles are cached on disk and re-used offline; with tiles "
            "off, geometries are drawn on a plain background and no "
            "request leaves the machine.",
        )
        enable_row = Adw.SwitchRow(
            title=_("Show Map Tiles"),
            subtitle=_("Fetch background tiles from the tile server below"),
        )
        enable_row.set_active(settings.map_tiles_enabled)
        enable_row.connect(
            "notify::active",
            lambda row, *_: store.update(map_tiles_enabled=row.get_active()),
        )
        group.add(enable_row)

        url_row = Adw.EntryRow(title=_("Tile URL"))
        url_row.set_text(settings.map_tile_url)
        url_row.set_show_apply_button(True)
        url_row.connect(
            "apply",
            lambda row, *_: store.update(map_tile_url=row.get_text().strip()),
        )
        group.add(url_row)

        credit_row = Adw.EntryRow(title=_("Attribution"))
        credit_row.set_text(settings.map_attribution)
        credit_row.set_show_apply_button(True)
        credit_row.connect(
            "apply",
            lambda row, *_: store.update(
                map_attribution=row.get_text().strip()
            ),
        )
        group.add(credit_row)

        cap_row = Adw.SpinRow.new_with_range(1, 100_000, 100)
        cap_row.set_title(_("Maximum Features Drawn"))
        cap_row.set_subtitle(
            "Past this many geometries the map says \"showing N of M\""
        )
        cap_row.set_value(settings.map_max_features)
        cap_row.connect(
            "notify::value",
            lambda row, *_: store.update(
                map_max_features=int(row.get_value())
            ),
        )
        group.add(cap_row)
        return group

    def _transfer_group(self) -> Adw.PreferencesGroup:
        """The XML transfer actions, which used to be a section of the
        main menu. They act on the window the dialog was opened from,
        through its win.* actions, so the whole group hides itself on
        the welcome page and the launcher, which have no workspace.

        The root is only known once the dialog is on screen, hence the
        check on map rather than in the constructor."""
        group = Adw.PreferencesGroup(
            title=_("Workspace Transfer"),
            description="Portable XML files. Passwords are left out "
            "unless you ask for them, and importing never overwrites a "
            "connection that is already there.",
            visible=False,
        )
        rows = (
            ("Export Workspace…", "win.export-workspace"),
            ("Export Connections…", "win.export-connections"),
            ("Import Connections…", "win.import-connections"),
        )
        for title, action in rows:
            row = Adw.ActionRow(title=title, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", self._run_transfer, action)
            group.add(row)
        # A hidden widget is never mapped, so the check hangs off the
        # dialog, which always is.
        self.connect("map", lambda *_: self._show_if_workspace(group))
        return group

    def _show_if_workspace(self, group: Adw.PreferencesGroup) -> None:
        root = self.get_root()
        group.set_visible(
            isinstance(root, Gtk.ApplicationWindow)
            and root.lookup_action("export-workspace") is not None
        )

    def _run_transfer(self, row: Adw.ActionRow, action: str) -> None:
        """Close first: the file chooser and any error the transfer
        reports belong to the window, and would otherwise open behind
        this dialog."""
        root = self.get_root()
        self.close()
        if root is not None:
            root.activate_action(action, None)

    # Keyboard shortcuts

    def _shortcuts_page(self) -> Adw.PreferencesPage:
        """One row per action in the keymap registry (frontend/
        keymap.py), grouped the same way as the shortcuts window, each
        with a button that captures the next keypress as its new
        binding."""
        page = Adw.PreferencesPage(
            title=_("Shortcuts"), icon_name="input-keyboard-symbolic"
        )
        groups: dict[str, Adw.PreferencesGroup] = {}
        for action in keymap.ACTIONS:
            group = groups.get(action.group)
            if group is None:
                group = Adw.PreferencesGroup(title=action.group)
                groups[action.group] = group
                page.add(group)
            group.add(_ShortcutRow(action))
        return page

    def _open_backup_window(self, *_args) -> None:
        window = BackupWindow()
        root = self.get_root()
        if isinstance(root, Gtk.Window):
            window.set_transient_for(root)
        window.present()

    # Language servers

    def _lsp_page(self, settings: Settings) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title=_("Language Servers"),
            icon_name="utilities-terminal-symbolic",
        )

        toggle_group = Adw.PreferencesGroup()
        enable_row = Adw.SwitchRow(
            title=_("Enable Language Servers"),
            subtitle=_("Schema-aware completion in query consoles"),
        )
        enable_row.set_active(settings.lsp_enabled)
        enable_row.connect(
            "notify::active",
            lambda row, *_: store.update(lsp_enabled=row.get_active()),
        )
        toggle_group.add(enable_row)
        page.add(toggle_group)

        defaults_group = Adw.PreferencesGroup(
            title=_("Default Server"),
            description="What a console set to “LSP: auto” uses, per "
            "database kind. Automatic keeps the built-in choice "
            "(plugins, then known servers).",
        )
        enable_row.bind_property(
            "active", defaults_group, "sensitive",
            GObject.BindingFlags.SYNC_CREATE,
        )
        available = lsp_servers.available_servers()
        for kind, title in _LSP_KINDS:
            defaults_group.add(self._default_row(kind, title, available))
        page.add(defaults_group)
        return page

    def _default_row(
        self, kind: str, title: str, available: list[str]
    ) -> Adw.ComboRow:
        current = store.settings.lsp_defaults.get(kind, lsp_servers.AUTO)
        choices = [lsp_servers.AUTO, lsp_servers.NONE, *available]
        if current not in choices:  # a saved server no longer installed
            choices.append(current)
        labels = ["Automatic", "Off", *choices[2:]]
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
        row.set_selected(choices.index(current))

        def changed(combo_row, *_args) -> None:
            defaults = dict(store.settings.lsp_defaults)
            defaults[kind] = choices[combo_row.get_selected()]
            store.update(lsp_defaults=defaults)

        row.connect("notify::selected", changed)
        return row


class _ShortcutRow(Adw.ActionRow):
    """One action's binding, editable in place. Clicking "Set Shortcut"
    puts the row in capture mode: the next keypress becomes the new
    accelerator, unless it's the text editor's own key (RESERVED) or
    already bound to another action, in which case it's rejected with
    a toast and the old binding stays. Backspace clears the binding;
    Escape cancels the capture without changing anything."""

    def __init__(self, action: keymap.Action) -> None:
        super().__init__(title=action.label)
        self._action = action

        self._keys = Gtk.Label(css_classes=["dim-label", "monospace"])
        self.add_suffix(self._keys)

        self._reset = Gtk.Button(
            icon_name="edit-undo-symbolic", css_classes=["flat"],
            valign=Gtk.Align.CENTER, tooltip_text=_("Reset to default"),
        )
        self._reset.connect(
            "clicked", lambda *_: self._apply(action.default)
        )
        self.add_suffix(self._reset)

        self._capture = Gtk.ToggleButton(
            label=_("Set Shortcut"), css_classes=["flat"],
            valign=Gtk.Align.CENTER,
        )
        self._capture.connect("toggled", self._on_toggled)
        keys_controller = Gtk.EventControllerKey()
        keys_controller.connect("key-pressed", self._on_key_pressed)
        self._capture.add_controller(keys_controller)
        self.add_suffix(self._capture)

        self._refresh()

    def _refresh(self) -> None:
        accel = keymap.effective(self._action.id)
        self._keys.set_label(keymap.spell(accel) if accel else "Unset")
        self._reset.set_visible(accel != self._action.default)

    def _on_toggled(self, button: Gtk.ToggleButton) -> None:
        button.set_label("Press keys…" if button.get_active() else "Set Shortcut")
        if button.get_active():
            button.grab_focus()

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        if not self._capture.get_active():
            return False
        if keyval == Gdk.KEY_Escape:
            self._capture.set_active(False)
            return True
        mask = Gtk.accelerator_get_default_mod_mask()
        if keyval == Gdk.KEY_BackSpace and not (state & mask):
            self._apply("")
            self._capture.set_active(False)
            return True
        # A bare modifier key-press (Ctrl on its own, etc.) isn't a
        # complete accelerator yet — keep listening.
        if keyval in _MODIFIER_KEYVALS:
            return True
        if not Gtk.accelerator_valid(keyval, state & mask):
            feedback.toast(self, "That key can't be used in a shortcut")
            return True
        accelerator = Gtk.accelerator_name(keyval, state & mask)
        self._capture.set_active(False)
        if keymap.is_reserved(accelerator):
            feedback.toast(
                self,
                f"{keymap.spell(accelerator)} is reserved by the text "
                "editor and can't be reassigned",
            )
            return True
        other = keymap.conflict(self._action.id, accelerator)
        if other is not None:
            feedback.toast(
                self,
                f"{keymap.spell(accelerator)} is already used by "
                f"“{other.label}”",
            )
            return True
        self._apply(accelerator)
        return True

    def _apply(self, accelerator: str) -> None:
        keymap_overrides = dict(store.settings.keymap)
        if accelerator == self._action.default:
            keymap_overrides.pop(self._action.id, None)
        else:
            keymap_overrides[self._action.id] = accelerator
        store.update(keymap=keymap_overrides)
        self._refresh()


_MODIFIER_KEYVALS = {
    Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
    Gdk.KEY_Super_L, Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
}


def about_dialog() -> Adw.AboutDialog:
    return Adw.AboutDialog(
        application_name="sqlide",
        application_icon=APP_ID,
        version=__version__,
        developer_name="Jihed Mastouri",
        comments="A minimal SQL IDE for SQLite, MySQL, and PostgreSQL "
        "(GTK4 + libadwaita).",
        copyright="© 2026 Jihed Mastouri",
    )
