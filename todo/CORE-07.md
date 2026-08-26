## CORE-07 — Connection context menu: Close all related tabs

- **Status:** done

### Acceptance criteria

- [x] Right-clicking a connection shows **Close all related tabs**, with the
      count (e.g. "Close all 7 related tabs").
- [x] Closes every tab belonging to that connection — table tabs, query consoles,
      properties/info views — and nothing belonging to other connections.
- [x] Tabs with unsaved work (edited query text, pending grid edits) prompt once,
      listing them, with save / discard / cancel.
- [x] Disabled when the connection has no open tabs.

### Notes

- The item sits below **Disconnect** on the connection row's context
  menu and carries the count in its label, because that is what "all
  related tabs" means: the sidebar asks the window
  (`count_connection_tabs`) while it builds the menu. One tab reads
  "Close the 1 related tab". `Sidebar.set_menu_node` derives the
  enabled state from the same count, so a connection with nothing open
  has a dead item rather than a missing one.
- Which tabs are "related" is the same question `_page_connection`
  already answers for the status bar and the disconnect banners:
  consoles follow their connection dropdown, every other tab its
  profile. So table tabs, properties/info views, definition, index,
  graph, builder and CLI tabs are all included, in every pane and
  pop-out window, and tabs on other connections are untouched.
- Unsaved work is a duck-typed `unsaved_work()` on the tab, returning
  the phrase the confirmation lists it under: `QueryConsole` compares
  its editor against the text last written to disk ("unsaved query
  text" for a console that never had a file), `TableTab` counts its
  pending cell edits. One `Adw.AlertDialog` lists them all with Cancel
  / Discard / Save.
- **Save** calls `save_unsaved_work()` on each listed tab: a console
  writes its editor back to its file, or to a scratch `.sql` file when
  it never had one (the trick `_open_in_text_editor` already used), so
  Save never stops to ask a file-chooser question per tab; a table tab
  applies its pending updates directly, without the usual
  `UpdatePreviewDialog` — the user was just shown the tabs and asked
  once, and asking again per tab would be the same question twice.
- A console with an open transaction normally holds its own close
  behind a confirmation. During this bulk close (`_closing_connection`
  is set to the connection's name) that dialog is skipped and the
  transaction is rolled back instead: "prompt once" means once.
