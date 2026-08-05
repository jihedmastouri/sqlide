"""Preferences dialog and about dialog.

Both are opened through the app.preferences / app.about actions (menu
button in the main window and launcher headers). Every row writes to
the settings store on change — there is no Apply step — and the
application, subscribed to the store, restyles live (theme, editor
font). LSP defaults only affect servers started after the change;
already-running servers keep their session.
"""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from sqlide import APP_ID, __version__
from sqlide.backend.settings import THEMES, Settings, store
from sqlide.backend.sql_risk import CONFIRM_MODES
from sqlide.frontend.backup_dialog import BackupWindow
from sqlide.lsp import servers as lsp_servers

# Connection kinds that get a default-LSP row, with display titles.
_LSP_KINDS = (
    ("sqlite", "SQLite"),
    ("mysql", "MySQL"),
    ("postgres", "PostgreSQL"),
    ("jdbc", "JDBC"),
)
_THEME_LABELS = ("Follow System", "Light", "Dark")
# Parallel to sql_risk.CONFIRM_MODES.
_CONFIRM_LABELS = (
    "Always",
    "Outside Development",
    "Never",
)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self) -> None:
        super().__init__()
        self.add(self._general_page(store.settings))
        self.add(self._lsp_page(store.settings))

    # General: appearance

    def _general_page(self, settings: Settings) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="General", icon_name="preferences-system-symbolic"
        )
        group = Adw.PreferencesGroup(title="Appearance")

        theme_row = Adw.ComboRow(
            title="Theme", model=Gtk.StringList.new(list(_THEME_LABELS))
        )
        theme_row.set_selected(THEMES.index(settings.theme))
        theme_row.connect(
            "notify::selected",
            lambda row, *_: store.update(theme=THEMES[row.get_selected()]),
        )
        group.add(theme_row)

        font_row = Adw.SpinRow.new_with_range(6, 32, 1)
        font_row.set_title("Editor Font Size")
        font_row.set_subtitle("In points, for the SQL editor")
        font_row.set_value(settings.editor_font_size)
        font_row.connect(
            "notify::value",
            lambda row, *_: store.update(
                editor_font_size=int(row.get_value())
            ),
        )
        group.add(font_row)

        vim_row = Adw.SwitchRow(
            title="Vim Mode",
            subtitle="Modal Vim editing in SQL editors "
            "(needs GtkSourceView)",
        )
        vim_row.set_active(settings.vim_mode)
        vim_row.connect(
            "notify::active",
            lambda row, *_: store.update(vim_mode=row.get_active()),
        )
        group.add(vim_row)

        page.add(group)

        safety = Adw.PreferencesGroup(
            title="Safety",
            description="How much friction a DROP, TRUNCATE, DELETE or "
            "UPDATE gets before it runs. Production connections always "
            "ask for the object's name on the destructive ones.",
        )
        confirm_row = Adw.ComboRow(
            title="Confirm Destructive Statements",
            model=Gtk.StringList.new(list(_CONFIRM_LABELS)),
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

        backup_group = Adw.PreferencesGroup(title="Backup")
        backup_row = Adw.ActionRow(
            title="Backup &amp; Restore…",
            subtitle="Export or restore settings and workspaces",
            activatable=True,
        )
        backup_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        backup_row.connect("activated", self._open_backup_window)
        backup_group.add(backup_row)
        page.add(backup_group)
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
            title="Language Servers",
            icon_name="utilities-terminal-symbolic",
        )

        toggle_group = Adw.PreferencesGroup()
        enable_row = Adw.SwitchRow(
            title="Enable Language Servers",
            subtitle="Schema-aware completion in query consoles",
        )
        enable_row.set_active(settings.lsp_enabled)
        enable_row.connect(
            "notify::active",
            lambda row, *_: store.update(lsp_enabled=row.get_active()),
        )
        toggle_group.add(enable_row)
        page.add(toggle_group)

        defaults_group = Adw.PreferencesGroup(
            title="Default Server",
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
