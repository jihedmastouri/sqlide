"""Placeholders in SQL statements, for the console's value prompt.

The console scans the statements it is about to run for named
(:name) and positional (?) placeholders; when any are found it asks
for their values first. The same character-scanner approach as
sql_split: strings, quoted identifiers and comments are skipped, so a
":name" inside a string literal is not a placeholder. `::` (Postgres
casts) never starts one. Positional markers are numbered ?1, ?2, …
in order of appearance within one statement.

Values are substituted into the SQL as literals before execution
(Connector.execute takes a single SQL string): NULL/TRUE/FALSE (any
case) and plain numbers go in bare, everything else as a
single-quoted string with '' escaping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_BARE_WORDS = ("null", "true", "false")


@dataclass(frozen=True)
class Placeholder:
    name: str  # ":limit" -> "limit"; positional -> "?1", "?2", …
    start: int  # offset of the marker in the statement
    end: int  # offset just past the marker


def find_placeholders(sql: str) -> list[Placeholder]:
    """Placeholders in one statement, in order of appearance."""
    found: list[Placeholder] = []
    positional = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]
        if two == "--":
            i = sql.find("\n", i)
            i = n if i == -1 else i + 1
        elif two == "/*":
            i = sql.find("*/", i + 2)
            i = n if i == -1 else i + 2
        elif ch in ("'", '"', "`"):
            i += 1
            while i < n and sql[i] != ch:
                i += 1
            i += 1
        elif two == "::":  # Postgres cast, not a placeholder
            i += 2
        elif ch == ":":
            match = _NAME_RE.match(sql, i + 1)
            if match:
                found.append(Placeholder(match.group(), i, match.end()))
                i = match.end()
            else:
                i += 1
        elif ch == "?":
            positional += 1
            found.append(Placeholder(f"?{positional}", i, i + 1))
            i += 1
        else:
            i += 1
    return found


def substitute(sql: str, values: dict[str, str]) -> str:
    """Replace every placeholder with the literal form of its value
    (missing names become NULL)."""
    result = sql
    for ph in reversed(find_placeholders(sql)):
        literal = as_literal(values.get(ph.name, ""))
        result = result[: ph.start] + literal + result[ph.end :]
    return result


def as_literal(value: str) -> str:
    text = value.strip()
    if not text:
        return "NULL"
    if text.lower() in _BARE_WORDS:
        return text.upper()
    if _NUMBER_RE.match(text):
        return text
    return "'" + value.replace("'", "''") + "'"
