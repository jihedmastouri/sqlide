---
title: Configuration Files
description: Where config lives, the format, and every key you can set.
order: 8
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
| `editor_font_size` | integer ≥ 1 | `11` | SQL editor font size, in points. |
| `vim_mode` | boolean | `false` | Modal Vim editing in SQL editors (GtkSourceView only). |
| `confirm_destructive` | `"always"` \| `"non-dev"` \| `"never"` | `"non-dev"` | When a destructive statement asks first. `"non-dev"` runs development connections without a prompt. |
| `max_result_rows` | integer ≥ 0 | `5000` | Row cap for a console or preview statement. `0` means no cap. |
| `time_zone` | `"local"` \| `"utc"` \| `"server"` | `"local"` | Which zone a database session reports timestamps in. `"server"` takes whatever the server is set to. |
| `lsp_enabled` | boolean | `true` | Master switch for completion language servers. |
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
```

## `workspaces/<id>/state.json`

Session state, rewritten constantly: `tabs`, `selected_tab`,
`history` (capped at 200 entries), `placeholders` and `saved_filters`.
Delete it and the workspace opens with no tabs and no history —
nothing else is lost. There is no reason to edit it by hand and no
reason to commit it.
