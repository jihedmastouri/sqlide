## CORE-09 — Notes panel in the right sidebar

- **Status:** todo
- **Depends on:** CORE-13

### Goal

A Notes section in the right sidebar for free-form notes attached to a
connection, a table, or nothing in particular.

### Acceptance criteria

- [ ] Notes section lists notes with title, scope badge and last-modified date.
- [ ] Filter control: **All**, **This connection**, **This table** — plus a text
      filter over title and body.
- [ ] "Add note" opens a modal text editor (rich text or markdown — pick one and
      state it in the ticket); it supports at minimum headings, bold/italic,
      lists and code blocks.
- [ ] A new note defaults its scope to the currently selected object and the
      scope can be changed before saving.
- [ ] Notes can be edited and deleted, with delete confirmation.
- [ ] Notes are stored as files on disk per CORE-13 so they can be committed to
      git; the storage path is documented.
- [ ] A note whose target object no longer exists is kept and marked orphaned,
      never silently dropped.


