"""First-run home page.

Shown once, when there is no workspace on file at all (see
application.py — every later launch goes straight into the workspace
you were last in). It is the one screen that has to introduce the app
rather than get out of its way, so it does more than the workspace
list does: what sqlide is, what it does for you, and the single form
that gets you inside — name, colour, Create.

Deliberately not the launcher with different words. The launcher is a
switcher for people who already have workspaces and know what one is;
this page is for someone who has just opened the app and does not.
Once a workspace exists this window is never shown again, which is the
point: an intro you have to walk past every morning is a toll booth.
"""

from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gdk, Gtk

from sqlide import APP_ID
from sqlide.backend import identity
from sqlide.backend.workspaces import Workspace
from sqlide.frontend import identity as identity_ui
from sqlide.frontend import transfer
from sqlide.frontend.util import (
    describe,
    main_menu_button,
    open_workspace_from,
)

TAGLINE = "A minimal SQL IDE for SQLite, MySQL and PostgreSQL."

# The pitch, in the app's own voice: what it does, not what it is
# built with. Icons are ones the app already relies on elsewhere.
_FEATURES = (
    (
        "network-server-symbolic",
        "Every engine on the job",
        "SQLite, MySQL and PostgreSQL natively, and a JDBC bridge for "
        "the one database nobody warned you about.",
    ),
    (
        "document-edit-symbolic",
        "Data you can edit",
        "Page through a table and fix a cell in place. Edits go out as "
        "primary-key UPDATEs; rows without a key stay read-only.",
    ),
    (
        "utilities-terminal-symbolic",
        "Consoles that know your schema",
        "Completion from the live database, Ctrl+Enter to run, and a "
        "history of everything that ran — failures included.",
    ),
    (
        "view-grid-symbolic",
        "Workspaces, colour-coded",
        "Connections and open tabs remembered per workspace. Production "
        "wears its own colour and asks before destructive statements run.",
    ),
)


def app_icon(size: int) -> Gtk.Image:
    """The app icon at `size` px. Installed builds have it in the icon
    theme; a run from a source checkout does not, so fall back to the
    file the packaging installs, and to a stock icon if even that is
    missing (an installed-without-data-files run)."""
    display = Gdk.Display.get_default()
    theme = Gtk.IconTheme.get_for_display(display) if display else None
    if theme is not None and theme.has_icon(APP_ID):
        image = Gtk.Image.new_from_icon_name(APP_ID)
    else:
        svg = (
            Path(__file__).resolve().parents[2]
            / "data"
            / "icons"
            / f"{APP_ID}.svg"
        )
        image = (
            Gtk.Image.new_from_file(str(svg))
            if svg.exists()
            else Gtk.Image.new_from_icon_name("network-server-symbolic")
        )
    image.set_pixel_size(size)
    return image


class WelcomeWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Welcome to sqlide")
        self.set_default_size(760, 820)

        header = Adw.HeaderBar()
        header.add_css_class("flat")
        # No title: the hero underneath says it once, larger.
        header.set_title_widget(Gtk.Label())
        # Coming from another machine is the other way to start, so it
        # is on the page — but in the header, where the launcher keeps
        # it too, rather than below the fold under the pitch.
        import_button = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="document-open-symbolic", label="Import…"
            )
        )
        import_button.add_css_class("flat")
        describe(
            import_button,
            "Import a workspace exported from another machine (XML)",
        )
        import_button.connect("clicked", lambda *_: self._import())
        header.pack_start(import_button)
        header.pack_end(main_menu_button())

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
            margin_top=6,
            margin_bottom=24,
            margin_start=12,
            margin_end=12,
        )
        body.append(self._hero())
        body.append(self._form())
        body.append(self._features())

        scroller = Gtk.ScrolledWindow(
            child=Adw.Clamp(
                child=body, maximum_size=660, tightening_threshold=560
            ),
            vexpand=True,
        )
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroller)
        self._toasts = Adw.ToastOverlay(child=view)
        self.set_content(self._toasts)

        # Type the name and press Enter: the whole first run, if you
        # are in a hurry.
        self.connect("map", self._on_map)

    def _on_map(self, *_args) -> None:
        self._name.grab_focus()
        if (error := self.get_application().take_store_error()) is not None:
            self._toast(error)

    # Sections

    def _hero(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
        )
        icon = app_icon(88)
        # Centred, so the rounded shadow hugs the icon instead of
        # stretching across the whole clamp.
        icon.set_halign(Gtk.Align.CENTER)
        icon.add_css_class("welcome-icon")
        box.append(icon)

        title = Gtk.Label(label="sqlide")
        title.add_css_class("title-1")
        box.append(title)

        tagline = Gtk.Label(label=TAGLINE, wrap=True, justify=Gtk.Justification.CENTER)
        tagline.add_css_class("title-4")
        tagline.add_css_class("dim-label")
        box.append(tagline)
        return box

    def _form(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        group = Adw.PreferencesGroup(
            title="Create your first workspace",
            description="A workspace holds a set of connections and "
            "remembers the tabs you left open in them. One per project, "
            "or per client, is the usual shape.",
        )
        self._name = Adw.EntryRow(title="Workspace name")
        self._name.connect("entry-activated", lambda *_: self._create())
        group.add(self._name)
        self._color = identity_ui.ColorRow(
            subtitle="Tints this workspace's window, so you can tell "
            "two of them apart at a glance"
        )
        self._color.set_color(identity.NONE)
        group.add(self._color)
        box.append(group)

        create = Gtk.Button(label="Create Workspace", halign=Gtk.Align.CENTER)
        create.add_css_class("suggested-action")
        create.add_css_class("pill")
        create.connect("clicked", lambda *_: self._create())
        box.append(create)
        return box

    def _features(self) -> Gtk.Widget:
        # A flow box, not a grid: two cards side by side on a roomy
        # window, one per line when it is narrow.
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=2,
            min_children_per_line=1,
            homogeneous=True,
            row_spacing=12,
            column_spacing=12,
        )
        for icon_name, title, text in _FEATURES:
            flow.append(_feature_card(icon_name, title, text))
        return flow

    # Actions

    def _create(self) -> None:
        name = self._name.get_text().strip() or "My Workspace"
        try:
            workspace = self.get_application().workspace_store.create(
                name, self._color.get_color()
            )
        except Exception as exc:
            # Nothing was created, so the page is still usable: say what
            # went wrong and let them try again.
            self._toast(f"Could not create the workspace: {exc}")
            return
        open_workspace_from(self, workspace)

    def _import(self) -> None:
        transfer.import_workspace(self, self._imported, self._toast)

    def _imported(self, workspace: Workspace) -> None:
        store = self.get_application().workspace_store
        store.workspaces.append(workspace)
        try:
            store.save(workspace)
        except Exception as exc:
            store.workspaces.remove(workspace)
            self._toast(f"Could not save the imported workspace: {exc}")
            return
        open_workspace_from(self, workspace)

    def _toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message))


def _feature_card(icon_name: str, title: str, text: str) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.add_css_class("card")
    box.add_css_class("welcome-card")

    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(24)
    icon.set_halign(Gtk.Align.START)
    icon.add_css_class("accent")
    box.append(icon)

    heading = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=24)
    heading.add_css_class("heading")
    box.append(heading)

    # max_width_chars, or the label's natural width is the whole
    # sentence on one line — which is wider than half the page, so the
    # flow box gives up and stacks the cards one per row.
    caption = Gtk.Label(
        label=text,
        xalign=0,
        wrap=True,
        max_width_chars=30,
        vexpand=True,
        valign=Gtk.Align.START,
    )
    caption.add_css_class("caption")
    caption.add_css_class("dim-label")
    box.append(caption)
    return box
