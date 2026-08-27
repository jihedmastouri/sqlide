---
title: Configuration Files
description: Where config lives, the format, and every key you can set.
order: 9
---

Everything sqlide remembers is a file you can read, diff and edit —
by hand, or with an agent. The app reads those files at startup, picks
up edits made while it runs, and writes changes made in the UI back
without losing your comments.

## Where it lives

The first of these that applies wins:

1. `--config-dir PATH` on the command line — `sqlide --config-dir
   ./sqlide-config`. Useful for a project-local config kept in that
   project's repository.
2. `$SQLIDE_CONFIG_DIR`.
3. `$XDG_CONFIG_HOME/sqlide`, when `XDG_CONFIG_HOME` is set. Honoured
   on every OS, so a script can redirect config the same way anywhere.
4. The platform default:

   | OS | Path |
   | --- | --- |
   | Linux, BSD | `~/.config/sqlide/` |
   | macOS | `~/Library/Application Support/sqlide/` |
   | Windows | `%APPDATA%\sqlide\` |

## The layout

```
<config>/
├── settings.toml            # app and UI preferences
├── notes.toml               # free-form notes (side panel -> Notes)
├── snippets.json            # saved SQL snippets (content, not config)
├── saved_queries.json       # saved queries      (content, not config)
├── backups.json             # backup jobs, destinations and run history
├── lsp/                     # optional language-server plugin scripts
└── workspaces/
    └── <workspace-id>/
        ├── workspace.toml   # id, name, identity colour
        ├── connections.toml # the connection definitions
        └── state.json       # open tabs, history, filters (session state)
```

The split is there so unrelated changes don't collide in git: adding a
connection touches one file, changing the theme another, and the churn
of open tabs and query history is confined to `state.json`, which you
can safely add to `.gitignore`.

### Why TOML

TOML won. It takes comments, it diffs a line at a time, and Python
reads it out of the standard library. Config files are TOML; sqlide
rewrites them key by key, so comments and key order you put in survive
a change made in the UI.

Two things are deliberately not TOML:

- **`state.json` and the saved-SQL files** — session state and SQL
  text, not configuration. They change constantly and nobody
  annotates them.
- **XML** — kept, but only where it earns its place: as the explicit
  import/export format for carrying a workspace to another machine
  (see [Import and Export](transfer)). It is not a config format here.

## Editing while the app runs

`notes.toml` is watched too: a note added or edited on disk shows up in
the side panel's Notes page without a restart.

`settings.toml` is watched: save it and the running app re-reads it and
applies the change — theme, font size, row cap, shortcuts and all.
Nothing in it needs a restart.

Workspace files (`workspace.toml`, `connections.toml`, `state.json`)
are read at startup only. An open window holds its connections and
tabs in memory and would write them back over your edit, so **restart
to apply** changes there.

A file that doesn't parse never takes the app down: the error is
reported with its file, line and key, that file falls back to defaults,
and everything else still loads. The same message goes to stderr, so
the headless `sqlide-backup` runner reports it too.

## Secrets

**Passwords are never written to these files** when a system keyring is
available. `password` and `ssh_password` are stored in the OS keyring
(see [Connection Security](connection-security)) keyed by workspace id
and connection name; `connections.toml` carries them as empty strings.
Only on a machine with no usable keyring — no `keyring` extra
installed, or no secret service running — do they fall back to plain
text in the file, exactly as in earlier versions.

So, to commit a config directory to git safely:

1. Install the keyring extra: `pip install "sqlide[keyring]"`, and
   check that `password = ""` in each `[[connection]]` before the first
   commit. If a password is sitting there in plain text, your machine
   has no keyring and you should not commit the file.
2. Ignore the churn and the machine-local state:

   ```gitignore
   workspaces/*/state.json
   backups.json
   ```

3. Remember the keyring is per machine. A checkout on another machine
   opens with every connection defined and each password asked for
   once.

## `settings.toml`

Every key, its type, and its default. An unknown key is left alone; a
key with a value outside its set is reported and falls back.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `theme` | `"system"` \| `"light"` \| `"dark"` | `"system"` | Colour scheme override. |
| `language` | `"system"` \| a shipped language code | `"system"` | Which language the UI is in. `"system"` follows the environment's locale. Overridden for one run by `--language CODE`. Applies at startup, so a change asks for a restart. See [Languages](#languages). |
| `editor_font_size` | integer ≥ 1 | `11` | SQL editor font size, in points. |
| `vim_mode` | boolean | `false` | Modal Vim editing in SQL editors (GtkSourceView only). |
| `confirm_destructive` | `"always"` \| `"non-dev"` \| `"never"` | `"non-dev"` | When a destructive statement asks first. `"non-dev"` runs development connections without a prompt. |
| `max_result_rows` | integer ≥ 0 | `5000` | Row cap for a console or preview statement. `0` means no cap. |
| `time_zone` | `"local"` \| `"utc"` \| `"server"` | `"local"` | Which zone a database session reports timestamps in. `"server"` takes whatever the server is set to. |
| `sql_keyword_case` | `"upper"` \| `"lower"` \| `"follow"` | `"upper"` | The case completion inserts a SQL keyword in. `"follow"` matches the prefix you typed — all lower case gives `select`, a leading capital gives `SELECT` — and falls back to upper case when nothing is typed yet. Keywords only: table, column and schema names keep the case the catalog reports, since a server may treat it as significant. |
| `sidebar_width` | integer | `280` | Width of the connections sidebar, in pixels. Clamped to 180–600; drag its inner edge to change it, double-click that edge to reset. |
| `side_panel_width` | integer | `340` | Width of the right side panel (Properties, Info, Notes, History…), in pixels. Clamped to 260–900; drag its inner edge to change it. |
| `monitor_interval` | integer 1–60 | `2` | Seconds between samples in the [monitoring](/docs/monitoring/) dashboard. The dashboard's own control writes here; storage keeps its separate 60-second timer. |
| `lsp_enabled` | boolean | `true` | Master switch for completion language servers. |
| `show_system_schemas` | boolean | `true` | Keep `information_schema` and the server's own catalog in the object tree. Shown dimmed and after the user's schemas; off hides them entirely. |
| `sidebar_follow_active_tab` | boolean | `true` | Highlight the active tab's object in the object tree, expanding the rows on the way to it and scrolling only when it is off screen. Off leaves the tree where you put it. |
| `map_tiles_enabled` | boolean | `true` | Whether the geo viewer fetches background map tiles. Off draws geometries on a plain grid and makes no network request at all. |
| `map_tile_url` | string | `"https://tile.openstreetmap.org/{z}/{x}/{y}.png"` | Slippy-map tile template for the geo viewer. Point it at your own tile server and nothing in the app talks to openstreetmap.org. Must be an `http(s)` URL containing `{z}`, `{x}` and `{y}`. |
| `map_attribution` | string | `"© OpenStreetMap contributors"` | The credit line drawn over the map. It belongs to whichever server the tiles come from; blank turns tiles off rather than dropping the credit. |
| `map_max_features` | integer ≥ 1 | `2000` | How many geometries one map draws before it stops and says "showing N of M". |
| `last_workspace` | string | `""` | Id of the workspace to reopen on startup. Empty, or a workspace that no longer exists, opens the first on file. |

Three tables of string-to-string:

| Table | Keys | Values |
| --- | --- | --- |
| `[lsp_defaults]` | a connection kind — `sqlite`, `mysql`, `postgres`, `jdbc` | `"auto"`, `"none"`, or a server name from the ones sqlide can launch. |
| `[mcp_defaults]` | `bind_host`, `row_limit`, `allow_query`, `auth_mode` | Last values of the MCP tab's form. The MCP token is never stored. |
| `[keymap]` | an action id (see the Shortcuts window) | An accelerator, in `Gtk.accelerator_parse()` syntax — `"<Control>t"`. `""` unbinds the action. Only actions you rebound appear here. |

```toml
# ~/.config/sqlide/settings.toml
theme = "dark"
editor_font_size = 12
max_result_rows = 5000

[keymap]
"win.run-query" = "<Control>Return"
```

## Languages

sqlide's interface is translatable, through gettext. Which language it
speaks is the first of these that resolves:

1. `--language CODE` on the command line — `sqlide --language fr`.
   Handy for checking a translation without touching your settings.
2. `language` in `settings.toml`, or the **Interface Language** row at
   the top of Preferences → General, which writes that key.
3. The system locale: `$LANGUAGE`, `$LC_ALL`, `$LC_MESSAGES`, `$LANG`.
4. English, which is what the strings in the source already say.

A language sqlide has no catalogue for is not an error — it simply
leaves the UI in English. Neither is a partial catalogue: a string the
translation does not carry falls back to English on its own, so a
translation is useful from its first line and never leaves a blank
label behind.

Preferences lists only the languages that actually ship with a
compiled catalogue, so nothing there is a promise the app cannot keep.

### What ships today

| Code | Language | State |
| --- | --- | --- |
| `en` | English | The source strings. Complete by definition. |
| `fr` | Français | **Partial and unreviewed.** Around 270 of 460 strings: menus, tabs, buttons, dialogs, the settings pages and every plural form. Written to prove the pipeline end to end, not by a native reviewer — corrections welcome. |

### Adding a language

Everything lives under `po/`, and one target drives the whole thing:

```
po/sqlide.pot                    # the template, extracted from the source
po/fr.po                         # one per language, the file you edit
sqlide/locale/fr/LC_MESSAGES/    # the compiled catalogue the app loads
```

1. `make i18n` — re-extracts `po/sqlide.pot` from every string marked
   in the source, merges it into each existing `.po`, and compiles the
   catalogues. Run it whenever a user-visible string changes.
2. Start your language from the template:

   ```sh
   msginit --locale=de --input=po/sqlide.pot --output=po/de.po
   ```

3. Translate `po/de.po` in any PO editor (Poedit, Gtranslator, an
   ordinary text editor). Leave a string empty rather than guessing —
   an empty one falls back to English, a wrong one does not.
   `msgstr[0]` / `msgstr[1]` are the plural forms; the header's
   `Plural-Forms` line says how many your language takes.
4. Add the code and the language's own name to `LANGUAGES` in
   `sqlide/i18n.py`, so Preferences can offer it.
5. `make i18n` again, then `sqlide --language de` to see it.

Two rules keep the strings translatable:

- Never build a sentence by concatenation, and never pick a plural
  with `if n == 1` — word order and plural rules are the translator's
  to decide. Use one format string with named placeholders, and
  `ngettext()` for anything counted.
- Numbers, dates and byte sizes go through the helpers in
  `sqlide/i18n.py` (`format_number`, `format_datetime`, `format_size`)
  rather than `f"{n:,}"`, which is English punctuation hard-coded.

Digit grouping and month names come from the machine's own locale
data. On a system where that locale is not built — common in a
container — sqlide keeps neutral formatting; the translations
themselves still work, since gettext does not need the locale.

## `notes.toml`

The notes shown in the side panel's **Notes** page: free-form Markdown
attached to a connection, a table, or nothing in particular. One
`[[note]]` table each, so a note is a few lines to diff and a file
worth committing.

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | string | Stable id, generated when the note is written. Leave it alone; a missing one gets a fresh id on load. |
| `title` | string | The row's title. An empty one becomes `"Untitled"`. |
| `body` | string | The note itself, in **Markdown** — headings, bold/italic, lists, fenced code blocks. The editor's toolbar only inserts those markers; nothing renders the body, so what you write is what the file holds. |
| `scope` | `"global"` \| `"connection"` \| `"table"` | What the note is about. An unknown scope is reported and read as `"global"`. |
| `connection` | string | The connection profile's name, for `"connection"` and `"table"` notes. |
| `table` | string | The table (or `schema.table`), for `"table"` notes. |
| `created`, `updated` | ISO-8601 string | Written by the app; `updated` orders the list, newest first. |

A note whose connection is not in the workspace any more is **kept and
badged "orphaned"** — never dropped — so deleting a connection, or
opening the config on a machine that has fewer of them, cannot lose
what you wrote.

```toml
# ~/.config/sqlide/notes.toml
[[note]]
id = "0f3c…"
title = "Retention"
body = "## orders\n\nRows older than 90 days are archived nightly."
scope = "table"
connection = "analytics-primary"
table = "public.orders"
created = "2026-08-26T10:04:00"
updated = "2026-08-26T10:04:00"
```

## `workspaces/<id>/workspace.toml`

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | string | The workspace id. It keys the keyring entries and `last_workspace`, so don't change it on a live config — rename the folder and the key together, or expect passwords to be asked for again. |
| `name` | string | Shown in the launcher and the window title. |
| `color` | identity colour or `"none"` | The window stripe and launcher dot. An unknown colour loads as `"none"`. |

## `workspaces/<id>/connections.toml`

One `[[connection]]` table per connection. Every key is optional except
`name` and `kind`; a key that doesn't apply to the kind is ignored.

| Key | Type | Default | Applies to |
| --- | --- | --- | --- |
| `name` | string | — | all — unique within the workspace |
| `kind` | `"sqlite"` \| `"mysql"` \| `"postgres"` \| `"jdbc"` | — | all |
| `color` | identity colour or `"none"` | `"none"` | all |
| `environment` | `"development"` \| `"staging"` \| `"production"` \| `"unset"` | `"unset"` | all — drives the destructive-action ladder |
| `file_path` | string | `""` | sqlite: the database file |
| `pragmas` | array of strings | `[]` | sqlite: PRAGMA defaults applied on every connect, one `"name = value"` an entry |
| `host` | string | `"localhost"` | mysql, postgres |
| `port` | integer | `0` | mysql, postgres — `0` means the engine default (3306 / 5432) |
| `user` | string | `""` | mysql, postgres, jdbc |
| `password` | string | `""` | **keyring-backed; leave empty** |
| `database` | string | `""` | mysql, postgres |
| `schema` | string | `""` | postgres: pinned as the session `search_path`; empty keeps the server's own |
| `jdbc_url` | string | `""` | jdbc, e.g. `jdbc:h2:/path/to/db` |
| `driver_class` | string | `""` | jdbc, e.g. `org.h2.Driver` |
| `jar_path` | string | `""` | jdbc: path to the driver jar |
| `ssl_mode` | `""` \| `"disable"` \| `"require"` \| `"verify-ca"` \| `"verify-full"` | `""` | mysql, postgres — `""` keeps the driver default |
| `ssl_ca`, `ssl_cert`, `ssl_key` | string | `""` | mysql, postgres: PEM file paths |
| `use_ssh` | boolean | `false` | mysql, postgres: connect through an SSH forward |
| `ssh_host` | string | `""` | when `use_ssh` |
| `ssh_port` | integer | `22` | when `use_ssh` |
| `ssh_user` | string | `""` | when `use_ssh` |
| `ssh_password` | string | `""` | **keyring-backed; leave empty** |
| `ssh_key_path` | string | `""` | when `use_ssh` |

```toml
# Connections for the Work workspace. Safe to commit: passwords live
# in the system keyring, not here.
[[connection]]
name = "prod"
kind = "postgres"
host = "db.internal"
port = 5432
user = "app"
database = "app"
password = ""        # keyring
environment = "production"
color = "red"        # so a prod tab never looks like a dev one

[[connection]]
name = "notes"
kind = "sqlite"
file_path = "/home/me/notes.db"
# Applied every time this connection opens. Written by Save as
# Defaults in the connection's PRAGMAs tab, and readable by hand:
# an unknown name or a value the pragma does not take is skipped
# with a message, and the rest still apply.
pragmas = ["foreign_keys = 1", "busy_timeout = 5000"]
```

## `workspaces/<id>/state.json`

Session state, rewritten constantly: `tabs`, `selected_tab`,
`history` (capped at 200 entries), `placeholders` and `saved_filters`.
Delete it and the workspace opens with no tabs and no history —
nothing else is lost. There is no reason to edit it by hand and no
reason to commit it.
