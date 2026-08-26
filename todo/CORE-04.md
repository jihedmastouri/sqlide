## CORE-04 — Table tab: Data / Properties toggle

- **Status:** todo
- **Depends on:** CORE-01
- **Blocks:** CORE-05, CORE-11

### Goal

An open table has a toggle between its data grid and a Properties view holding
everything about the table.

### Acceptance criteria

- [ ] A visible toggle (Data | Properties) on every open table tab.
- [ ] Properties contains, per engine capability: general info (owner, size, row
      estimate, comment), Columns, Constraints, Foreign keys, References,
      Indexes, Triggers, Partitions, Rules, Policies, Dependencies, Source
      /related functions, and full DDL.
- [ ] Sections a given engine doesn't support are omitted, not shown empty.
- [ ] Switching to Properties does not discard unsaved grid edits or lose the
      grid's scroll position/filters when switching back.
- [ ] Each row in a Properties section opens that child object's info view
      (CORE-01).

