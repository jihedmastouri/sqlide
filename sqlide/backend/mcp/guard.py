"""Read-only query validation for the MCP server's query tool.

Belt and braces on top of the driver-level read-only session the
instance opens (sqlite mode=ro, postgres default_transaction_read_only,
mysql SET SESSION TRANSACTION READ ONLY): a query is admitted only if

- it is exactly one statement (reusing the console's splitter),
- its first keyword is in the dialect's allowlist (SELECT/WITH/
  EXPLAIN, plus SHOW on MySQL; PRAGMA stays out — some PRAGMAs write),
- no write keyword appears anywhere in it. That last scan catches the
  nested forms the first keyword misses: PostgreSQL's data-modifying
  CTEs (WITH x AS (DELETE …) SELECT …), EXPLAIN ANALYZE over a write,
  SELECT … INTO, SELECT … FOR UPDATE. The scanned words are reserved
  in every supported dialect, so an unquoted column can't collide;
  quoted identifiers, strings, comments and dollar-quoted bodies are
  skipped.
"""

from __future__ import annotations

from sqlide.backend.sql_split import split_statements, tokens as split_tokens

_ALLOWED_FIRST: dict[str, tuple[str, ...]] = {
    "sqlite": ("SELECT", "WITH", "EXPLAIN"),
    "mysql": ("SELECT", "WITH", "EXPLAIN", "SHOW"),
    "postgres": ("SELECT", "WITH", "EXPLAIN"),
    "jdbc": ("SELECT", "WITH", "EXPLAIN"),
}

# Write keywords that can legally nest inside an allowed statement.
# Statement-level writes (CREATE, DROP, SET, VACUUM, …) can't nest, so
# the first-keyword allowlist already blocks them — keeping this set
# small avoids false positives on unquoted column names.
_WRITE_WORDS = frozenset({"INSERT", "UPDATE", "DELETE", "INTO"})


class GuardError(Exception):
    """The query is not provably read-only."""


def check_read_only(sql: str, dialect: str) -> str:
    """Validate `sql` for the given connection kind and return the
    single statement's text; raises GuardError otherwise."""
    statements = split_statements(sql)
    if not statements:
        raise GuardError("Empty query")
    if len(statements) > 1:
        raise GuardError("Only a single statement is allowed")
    text = statements[0].text
    words = _words(text)
    if not words:
        raise GuardError("Empty query")
    allowed = _ALLOWED_FIRST.get(dialect, _ALLOWED_FIRST["jdbc"])
    if words[0] not in allowed:
        raise GuardError(
            f"Only {'/'.join(allowed)} queries are allowed"
        )
    denied = _WRITE_WORDS.intersection(words)
    if denied:
        raise GuardError(
            "Write keyword not allowed in a read-only query: "
            + ", ".join(sorted(denied))
        )
    return text


def _words(sql: str) -> list[str]:
    """Uppercased bare words of one statement. Strings, quoted
    identifiers, dollar-quoted bodies and comments are skipped by the
    shared tokenizer — a quoted identifier must never be read as the
    keyword it spells."""
    return [t.word for t in split_tokens(sql) if not t.quoted]
