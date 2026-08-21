"""Central registry of every keyboard shortcut in the app.

One `Action` per bindable command: an id, a human label, a group (for
the shortcuts window and the preferences editor) and a default
accelerator in `Gtk.accelerator_parse()` syntax. This registry is the
single source backing application.py/window.py's Gio action accels,
the ad hoc key controllers in query_console.py and data_grid.py, and
the shortcuts window — so they can never drift apart, and a shortcut
edited in Preferences takes effect everywhere it's used.

`scope` says how an action's accelerator is actually installed:
- "app"/"win": a real Gio action (`app.foo` / `win.foo`); its
  accelerator is pushed into GTK with `Gtk.Application.
  set_accels_for_action()`, via `apply_app_accels()`.
- "local": not a Gio action — a widget's own `Gtk.EventControllerKey`
  or `Gtk.ShortcutController` checks the keypress against it directly,
  through `matches()`.

User overrides live in `Settings.keymap` (action id -> accelerator
string, only for actions the user rebound) and are layered over the
defaults here by `effective()`. Comparisons (`matches`, `is_reserved`,
`conflict`) all go through parsed `(keyval, modifiers)` pairs rather
than raw strings, since two spellings of the same combo ("<primary>c"
vs "<Control>c") parse identically but don't compare equal as text.

RESERVED holds the keys the text editor (GtkSourceView, Vim mode, and
the completion popup) owns outright. None of them can ever be assigned
to an app action — rebinding a shortcut must not take a keystroke the
editor depends on to work.
"""

from __future__ import annotations

from dataclasses import dataclass

from gi.repository import Gdk, Gtk

from sqlide.backend.settings import store


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    group: str
    default: str  # "" means unbound by default
    scope: str  # "app", "win", or "local"


ACTIONS: tuple[Action, ...] = (
    Action("app.preferences", "Preferences", "General", "<primary>comma", "app"),
    Action("app.shortcuts", "Keyboard shortcuts", "General", "<primary>question", "app"),
    Action("app.help", "Help", "General", "F1", "app"),
    Action("app.about", "About sqlide", "General", "", "app"),
    Action("app.show-launcher", "Switch workspace", "General", "<primary><shift>o", "app"),
    Action("window.close", "Close the window", "General", "<primary>w", "app"),
    Action("win.new-query", "New query console", "Tabs", "<primary>t", "win"),
    Action("win.new-cli", "New CLI console", "Tabs", "<primary><alt>t", "win"),
    Action("win.new-builder", "New query builder", "Tabs", "<primary><alt>b", "win"),
    Action("win.new-mcp", "New MCP server tab", "Tabs", "<primary><alt>m", "win"),
    Action("win.history", "Open history", "Tabs", "<primary><shift>h", "win"),
    Action("win.refresh-schema", "Refresh schema", "Tabs", "F5", "win"),
    Action("win.split-view", "Split the current tab", "Tabs", "<primary><alt>s", "win"),
    Action("win.close-tab", "Close the current tab", "Tabs", "<primary>F4", "win"),
    Action("win.close-other-tabs", "Close other tabs", "Tabs", "", "win"),
    Action("win.close-tabs-right", "Close tabs to the right", "Tabs", "", "win"),
    Action("win.close-all-tabs", "Close every tab", "Tabs", "<primary><shift>w", "win"),
    Action("win.export-workspace", "Export workspace", "Tabs", "", "win"),
    Action("win.export-connections", "Export connections", "Tabs", "", "win"),
    Action("win.import-connections", "Import connections", "Tabs", "", "win"),
    Action(
        "query.run", "Run the selection or the statement at the cursor",
        "Query console", "<primary>Return", "local",
    ),
    Action(
        "query.run-all", "Run every statement in the editor",
        "Query console", "<primary><shift>Return", "local",
    ),
    Action(
        "query.open-file", "Open a file in the editor",
        "Query console", "<primary>o", "local",
    ),
    Action(
        "query.save-file", "Save the editor to a file",
        "Query console", "<primary>s", "local",
    ),
    Action(
        "grid.copy", "Copy the selected cells",
        "Results and data grids", "<primary>c", "local",
    ),
)

_BY_ID = {a.id: a for a in ACTIONS}

# Keys the text editor owns outright: standard text editing (undo,
# clipboard, caret movement), the Vim IM context (every unmodified
# letter is a Vim command when it's on), and the completion popup's
# own navigation. None of these are ever offered as rebindable, and an
# attempt to assign one to an app action is rejected.
_RESERVED_STRINGS = (
    "<primary>z", "<primary>y", "<primary><shift>z",
    "<primary>a", "<primary>x", "<primary>v",
    "<primary>Home", "<primary>End",
    "<primary>Left", "<primary>Right", "<primary>Up", "<primary>Down",
    "<primary>space",
    "Tab", "Escape", "Return", "KP_Enter",
    "Up", "Down", "Left", "Right",
    "Menu", "<shift>F10",
)


def _parse(accelerator: str) -> tuple[int, int] | None:
    if not accelerator:
        return None
    ok, keyval, mods = Gtk.accelerator_parse(accelerator)
    return (keyval, int(mods)) if ok else None


RESERVED: frozenset[tuple[int, int]] = frozenset(
    parsed for s in _RESERVED_STRINGS if (parsed := _parse(s)) is not None
)

_MOD_LABELS = (
    (Gdk.ModifierType.CONTROL_MASK, "Ctrl+"),
    (Gdk.ModifierType.ALT_MASK, "Alt+"),
    (Gdk.ModifierType.SHIFT_MASK, "Shift+"),
    (Gdk.ModifierType.SUPER_MASK, "Super+"),
)
_KEY_LABELS = {
    "comma": ",", "question": "?", "equal": "=", "space": "Space",
    "Return": "Enter",
}


def effective(action_id: str) -> str:
    """The accelerator actually in effect: the user's override if they
    set one, else the built-in default."""
    override = store.settings.keymap.get(action_id)
    return override if override is not None else _BY_ID[action_id].default


def is_reserved(accelerator: str) -> bool:
    parsed = _parse(accelerator)
    return parsed is not None and parsed in RESERVED


def conflict(action_id: str, accelerator: str) -> Action | None:
    """The other action already bound to `accelerator`, or None. An
    empty accelerator (no binding) never conflicts."""
    parsed = _parse(accelerator)
    if parsed is None:
        return None
    for other in ACTIONS:
        if other.id != action_id and _parse(effective(other.id)) == parsed:
            return other
    return None


def matches(action_id: str, keyval: int, state) -> bool:
    """For the "local" key controllers that aren't Gio actions: does
    this key-press event match the action's effective accelerator?"""
    parsed = _parse(effective(action_id))
    if parsed is None:
        return False
    accel_keyval, accel_mods = parsed
    mask = Gtk.accelerator_get_default_mod_mask()
    return keyval == accel_keyval and int(state) & mask == accel_mods


def spell(accelerator: str) -> str:
    """"<primary><shift>Return" -> "Ctrl+Shift+Enter". Independent of
    which alias (<primary> vs <Control>) the string uses, so it reads
    the same whether it came from a default or a captured keypress."""
    parsed = _parse(accelerator)
    if parsed is None:
        return "Unset"
    keyval, mods = parsed
    mods = Gdk.ModifierType(mods)
    text = "".join(label for mask, label in _MOD_LABELS if mods & mask)
    name = Gdk.keyval_name(keyval) or "?"
    return text + _KEY_LABELS.get(name, name)


def grouped() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Every action's current (possibly user-set) accelerator, grouped
    for display — the shortcuts window and the help dialog's table."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for action in ACTIONS:
        groups.setdefault(action.group, []).append(
            (action.label, effective(action.id))
        )
    return tuple((group, tuple(items)) for group, items in groups.items())


def apply_app_accels(app) -> None:
    """Push every app/win-scoped action's effective accelerator into
    GTK. Called at startup and whenever the keymap setting changes."""
    for action in ACTIONS:
        if action.scope in ("app", "win"):
            accel = effective(action.id)
            app.set_accels_for_action(action.id, [accel] if accel else [])
