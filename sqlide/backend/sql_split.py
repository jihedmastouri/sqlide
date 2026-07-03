"""Split a SQL script into individual statements.

A small character scanner rather than a parser: it only needs to know
where statements end, so it tracks the contexts in which a semicolon
does NOT terminate one — single-quoted strings, double-quoted and
backtick-quoted identifiers, line comments and block comments. Escaped
quotes ('' or "") fall out naturally: the first quote closes the
context and the second reopens it.

Offsets are kept so the console can map the editor's cursor position
to the statement under it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Statement:
    text: str  # stripped statement text, without the trailing semicolon
    start: int  # offset of the first character in the original script
    end: int  # offset just past the statement (past its semicolon)


def split_statements(sql: str) -> list[Statement]:
    """Statements in `sql`, in order. Segments that hold only whitespace
    and comments are dropped; a comment preceding a statement stays part
    of it (comments are valid SQL, and a cursor inside the comment then
    maps to the statement it annotates)."""
    statements: list[Statement] = []
    seg_start = 0
    has_content = False  # any non-comment, non-whitespace char in segment
    i = 0
    n = len(sql)

    def close_segment(end: int) -> None:
        nonlocal seg_start, has_content
        if has_content:
            raw = sql[seg_start:end]
            text = raw.strip().removesuffix(";").strip()
            offset = seg_start + (len(raw) - len(raw.lstrip()))
            statements.append(Statement(text=text, start=offset, end=end))
        seg_start = end
        has_content = False

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
            has_content = True
            i += 1
            while i < n and sql[i] != ch:
                i += 1
            i += 1  # past the closing quote (or end on unterminated)
        elif ch == ";":
            close_segment(i + 1)
            i += 1
        else:
            if not ch.isspace():
                has_content = True
            i += 1
    close_segment(n)
    return statements


def statement_at(statements: list[Statement], offset: int) -> Statement | None:
    """The statement under `offset`: the first one that has not ended
    yet, so a cursor sitting right after a semicolon still picks the
    statement it terminates. Past the last statement, picks the last."""
    for statement in statements:
        if offset <= statement.end:
            return statement
    return statements[-1] if statements else None
