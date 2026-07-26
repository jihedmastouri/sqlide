# next steps — backlog

- [x] Connection & workspace management UI: edit/rename/remove a saved
      connection from the sidebar (connection_dialog.py's "edit" mode,
      pre-filled from the profile, applied in place so open tabs keep
      working); rename a workspace from a pencil button on its
      launcher row.
- [x] Let grid cell edits set a value to NULL: a "Set Cell to NULL"
      item on the cell's right-click menu (enabled only while editing
      is unlocked) — typing still can't tell NULL from "" in an
      EditableLabel, so this goes through on_edit(row, col, None)
      instead.
- [x] Move stored connection passwords out of plaintext JSON into the
      system keyring (new backend/secrets.py, optional `keyring`
      extra), falling back to plaintext when no keyring backend is
      available. Renaming/removing a connection moves/drops its
      keyring entries.
