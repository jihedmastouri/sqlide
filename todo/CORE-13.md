## CORE-13 — File-system configuration (git-friendly)

- **Status:** done
- **Blocks:** CORE-08 (persistence), CORE-09

### Problem

Config is not inspectable or editable outside the app. People want to keep it in
git, diff it, and let an agent edit it.

### Goal

All configuration lives in human-readable files that the app reads and writes.

### Approach

TOML as the primary format (comment-friendly, diffs cleanly). Support XML as an
alternative import/export if there's a real reason; otherwise state that TOML
won and drop XML. Split config into multiple files so unrelated changes don't
collide in git:

- app/UI preferences (theme, panel widths, editor settings)
- connections (definitions only)
- notes (CORE-09)
- per-connection overrides

### Acceptance criteria

- [x] Config location is documented per OS and overridable by env var and CLI flag.
- [x] The app reads config on start and writes back changes made in the UI,
      preserving comments and key order where the format allows.
- [x] Editing a file on disk while the app runs is picked up (watch + reload) or,
      if that's too invasive for a given key, surfaced as "restart to apply".
- [x] Invalid config produces a clear error naming the file, line and key, and
      falls back to defaults instead of crashing.
- [x] A documented schema/reference for every key, so an agent can edit safely.
- [x] **Secrets are never written in plaintext** — passwords stay in the OS
      keychain/secret store and the config references them; document how a config
      file can be committed to git safely.
- [x] Round-trip test: export → edit by hand → load → identical behaviour.

### Notes

TOML won: config files are TOML, written back through a small
comment-preserving writer (`backend/tomlwrite.py`). XML is kept only
where it already earned its place — the workspace import/export format
(`backend/exchange.py`, docs/transfer.md) — and is not a config format.

Session state (open tabs, history, placeholders, saved filters) stays
JSON in `workspaces/<id>/state.json`: it is churn, not configuration,
and keeping it out of the TOML is what makes the config files worth
committing. Per-connection overrides land with the features that need
them (CORE-08/CORE-09); the file split leaves room for them.

Settings are watched and reload live. Workspace files are read at
startup only — an open window would write its in-memory tabs and
connections back over an edit — so those are "restart to apply", as
documented.


