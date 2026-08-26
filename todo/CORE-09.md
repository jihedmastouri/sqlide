## CORE-09 — Notes panel in the right sidebar

- **Status:** done
- **Depends on:** CORE-13

### Goal

A Notes section in the right sidebar for free-form notes attached to a
connection, a table, or nothing in particular.

### Acceptance criteria

- [x] Notes section lists notes with title, scope badge and last-modified date.
- [x] Filter control: **All**, **This connection**, **This table** — plus a text
      filter over title and body.
- [x] "Add note" opens a modal text editor (rich text or markdown — pick one and
      state it in the ticket); it supports at minimum headings, bold/italic,
      lists and code blocks.
- [x] A new note defaults its scope to the currently selected object and the
      scope can be changed before saving.
- [x] Notes can be edited and deleted, with delete confirmation.
- [x] Notes are stored as files on disk per CORE-13 so they can be committed to
      git; the storage path is documented.
- [x] A note whose target object no longer exists is kept and marked orphaned,
      never silently dropped.

### Notes

**Markdown**, not rich text: the body is plain text and the editor's
toolbar inserts markers (heading, bold, italic, bullet and numbered
lists, fenced code block), so a note diffs and merges in git like the
rest of the config.

Storage is `notes.toml` in the config directory (CORE-13), one
`[[note]]` table per note, written through `tomlwrite.merge()` so hand
comments survive, and watched so an edit on disk shows up live.
Documented in docs/configuration.md.

Orphans are decided at display time (`Note.is_orphaned`) rather than
stored: the window hands the panel the workspace's connection names, a
note about a connection that is gone gets an "orphaned" badge, and
nothing is ever removed on the app's own initiative.
