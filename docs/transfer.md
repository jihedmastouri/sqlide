---
title: Import and Export
description: Moving workspaces and connections between machines as XML.
order: 7
---

sqlide keeps workspaces as [config files](configuration) under
`workspaces/<id>/`, keyed by a local id, with passwords in the system
keyring. That is the right shape for the app and the wrong
shape for carrying a setup to another machine — so transfers use a
separate, readable XML format.

## Exporting

From a workspace window, under **Preferences → General → Workspace
Transfer**:

- **Export Workspace…** — the workspace's name and colour, all its
  connections, and the filters saved against them.
- **Export Connections…** — the connections alone, for someone who
  already has a workspace to put them in.

If any connection has a password stored, sqlide asks first whether the
file should include it. **Without passwords** (the default) the file is
safe to send: each connection asks for its password once on the machine
it lands on. **Include passwords** writes them in plain text.

Open tabs, query history and the workspace's local id are never
exported — they describe the machine you left, not the databases.

## Importing

- **Import Workspace…** — the folder icon in the workspace list's
  header (the grid icon in the sidebar header opens it).
  The file always becomes a *new* workspace, with its own id and a name
  made unique against the ones already listed. An import can never
  overwrite a workspace you already have, so re-importing the same file
  is safe (it just makes another copy).
- **Import Connections…** — the same Preferences group; the
  connections are added to the open workspace. Names that collide are
  suffixed rather than replaced.

## The file

```xml
<sqlide format="1" exported="2026-08-06T10:00:00">
  <workspace name="Work" color="blue">
    <connection name="reports" kind="postgres" environment="production"
                color="red">
      <host>db.example.com</host>
      <port>5432</port>
      <user>app</user>
      <database>reports</database>
    </connection>
    <connection name="local" kind="sqlite">
      <file_path>/home/me/demo.db</file_path>
    </connection>
  </workspace>
</sqlide>
```

It is meant to be edited by hand and kept in version control:

- Only fields that differ from their default are written, so a
  connection is as short as it can be.
- Elements a version does not recognise are skipped rather than
  refused, so a file written by a newer sqlide still imports what the
  older one understands.
- The same file describes a whole workspace or just connections — the
  two import paths read the parts they need.

This makes provisioning a machine (or a team) a matter of committing
one XML file and importing it.
