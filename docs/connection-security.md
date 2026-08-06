---
title: Connection Security
description: Managing connections and where passwords are stored.
order: 8
---

## Managing connections

Right-click a connection in the sidebar for **Edit…** (the same form as
adding one, pre-filled — renaming it there is safe, open tabs keep
working) and **Remove…** (confirmed). Workspaces can be renamed too,
from a pencil button on their row in the launcher.

## Where passwords live

With the `keyring` extra installed and a backend available (GNOME
Keyring, KWallet, macOS Keychain, …), a connection's password and SSH
tunnel password are stored there instead of in the workspace's JSON
file, which keeps only a blank placeholder:

```sh
pip install "sqlide[keyring]"
```

Without a usable keyring — the extra isn't installed, or no backend is
running — sqlide falls back to plain text in the JSON file, as in
earlier versions. Nothing needs configuring either way; sqlide detects
what's available.

Keyring entries are per machine: copying a workspace file elsewhere
carries the blanked password field, not the secret, so the connection
needs its password re-entered once on the new machine.
