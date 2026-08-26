"""The SQLite metadata provider.

Driver-free on purpose (see db/metadata.py).

Shape: connection → object. One file is one database, so a database
level would be an empty step the user has to click through.

Nothing here has accounts: SQLite's permissions are the file's, which
means `grants` and `roles` are off and list_grants()/list_principals()
answer with empty lists rather than a screen that cannot be filled.
PRAGMAs are the settings surface instead (SQ-02).

There is no constraint catalog either, but the `constraints` capability
is on: the adapter reads them back off the PRAGMAs (see
sqlite/connector.py), so a table's properties can still list its keys.

Minimum supported version: SQLite 3.25 — the release that gave
ALTER TABLE … RENAME COLUMN, which the definition tab already relies on.
"""

from __future__ import annotations

from sqlide.backend.db.metadata import Capabilities, MetadataProvider


class SqliteMetadata(MetadataProvider):
    HIERARCHY = ("connection", "object")
    CAPABILITIES = Capabilities(pragmas=True, constraints=True)
