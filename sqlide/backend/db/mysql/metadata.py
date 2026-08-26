"""The MySQL metadata provider.

Driver-free on purpose (see db/metadata.py).

Shape: connection → database → object. In MySQL a schema *is* a
database, so there is no schema level to add — a second one would only
repeat the first.

Minimum supported server: MySQL 5.7, the oldest in the test matrix
(tests/conftest.py). Roles arrived in 8.0: on 5.7 the role catalog is
missing and list_users() answers with accounts alone, which is a
shorter list rather than an error (db/metadata.py `_safe`).
"""

from __future__ import annotations

from sqlide.backend.db.metadata import Capabilities, MetadataProvider


class MysqlMetadata(MetadataProvider):
    HIERARCHY = ("connection", "database", "object")
    CAPABILITIES = Capabilities(
        databases=True,
        procedures=True,
        events=True,
        grants=True,
        roles=True,
        partitions=True,
        constraints=True,
        account_hosts=True,
    )

    def _current_database(self) -> str:
        return getattr(self.connector, "database", "") or ""
