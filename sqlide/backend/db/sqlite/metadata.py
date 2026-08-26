"""The SQLite metadata provider.

Driver-free on purpose (see db/metadata.py).

Shape: connection → object. One file is one database, so a database
level would be an empty step the user has to click through.

Nothing here has accounts: SQLite's permissions are the file's, which
means `grants` and `roles` are off and list_grants()/list_principals()
answer with empty lists rather than a screen that cannot be filled.
PRAGMAs are the settings surface instead (SQ-02).

Minimum supported version: SQLite 3.25 — the release that gave
ALTER TABLE … RENAME COLUMN, which the definition tab already relies on.
"""

from __future__ import annotations

from sqlide.backend.db.metadata import Capabilities, MetadataProvider


class SqliteMetadata(MetadataProvider):
    HIERARCHY = ("connection", "object")
    CAPABILITIES = Capabilities(pragmas=True)
