"""Split a SQL script into individual statements.

A small character scanner rather than a parser: it only needs to know
where statements end, so it tracks the contexts in which a semicolon
does NOT terminate one — single-quoted strings, double-quoted and
backtick-quoted identifiers, dollar-quoted bodies ($$…$$ and
$tag$…$tag$, PostgreSQL), line comments and block comments. Escaped
quotes ('' or "") fall out naturally: the first quote closes the
context and the second reopens it.

Routine bodies are the one case where a semicolon sits bare inside a
statement: SQLite trigger and MySQL trigger/function/procedure bodies
(PostgreSQL wraps its bodies in dollar quotes). Inside a statement
whose first keyword is CREATE and that mentions TRIGGER, FUNCTION or
PROCEDURE, the scanner therefore counts BEGIN/CASE…END nesting and
only terminates at depth zero; END IF/LOOP/WHILE/REPEAT close their
uncounted openers and are consumed as pairs.

Offsets are kept so the console can map the editor's cursor position
to the statement under it.

`tokens()` shares the same context rules for the callers that need to
look *inside* one statement — the MCP read-only guard and the
destructive-statement classifier in sql_risk.py.
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
    first_word = ""  # first keyword of the segment ("" until seen)
    routine = False  # CREATE segment mentioning TRIGGER/FUNCTION/PROCEDURE
    depth = 0  # BEGIN/CASE…END nesting inside a routine segment
    i = 0
    n = len(sql)

    def close_segment(end: int) -> None:
        nonlocal seg_start, has_content, first_word, routine, depth
        if has_content:
            raw = sql[seg_start:end]
            text = raw.strip().removesuffix(";").strip()
            offset = seg_start + (len(raw) - len(raw.lstrip()))
            statements.append(Statement(text=text, start=offset, end=end))
        seg_start = end
        has_content = False
        first_word = ""
        routine = False
        depth = 0

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
        elif ch == "$" and (delim := _dollar_delimiter(sql, i)):
            has_content = True
            end = sql.find(delim, i + len(delim))
            i = n if end == -1 else end + len(delim)
        elif ch.isalpha() or ch == "_":
            has_content = True
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j].upper()
            if not first_word:
                first_word = word
            if first_word == "CREATE":
                if word in ("TRIGGER", "FUNCTION", "PROCEDURE"):
                    routine = True
                elif routine and word in ("BEGIN", "CASE"):
                    depth += 1
                elif routine and word == "END":
                    nxt, k = _peek_word(sql, j)
                    if nxt in ("IF", "LOOP", "WHILE", "REPEAT"):
                        j = k  # closes an uncounted opener
                    else:
                        if nxt == "CASE":  # MySQL's END CASE
                            j = k
                        depth = max(depth - 1, 0)
            i = j
        elif ch == ";":
            if depth == 0:
                close_segment(i + 1)
            i += 1
        else:
            if not ch.isspace():
                has_content = True
            i += 1
    close_segment(n)
    return statements


@dataclass(frozen=True)
class Token:
    """One identifier-ish token of a statement. `word` is the
    uppercased spelling of a bare word (used for keyword matching) and
    is empty for quoted identifiers, whose text is kept verbatim in
    `text` — `DELETE FROM "select"` names a table, not a keyword."""

    text: str
    word: str
    quoted: bool


def tokens(sql: str) -> list[Token]:
    """The bare words and quoted identifiers of `sql`, in order.

    String literals, comments and dollar-quoted bodies are skipped, so
    a keyword inside them can never be mistaken for the real thing.
    Numbers and punctuation are dropped: every caller here matches
    keywords and picks out object names, and neither needs them.
    """
    found: list[Token] = []
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
        elif ch == "'":  # string literal
            i += 1
            while i < n and sql[i] != ch:
                i += 1
            i += 1
        elif ch in ('"', "`"):  # quoted identifier
            j = i + 1
            while j < n and sql[j] != ch:
                j += 1
            found.append(Token(text=sql[i + 1 : j], word="", quoted=True))
            i = j + 1
        elif ch == "$" and (delim := _dollar_delimiter(sql, i)):
            end = sql.find(delim, i + len(delim))
            i = n if end == -1 else end + len(delim)
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            text = sql[i:j]
            found.append(Token(text=text, word=text.upper(), quoted=False))
            i = j
        else:
            i += 1
    return found


def _dollar_delimiter(sql: str, i: int) -> str:
    """The full dollar-quote delimiter starting at `i` ($$ or $tag$),
    or "" when this $ does not open one (e.g. a $1 placeholder)."""
    j = i + 1
    while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
        j += 1
    if j >= len(sql) or sql[j] != "$":
        return ""
    tag = sql[i + 1 : j]
    if tag and tag[0].isdigit():
        return ""
    return sql[i : j + 1]


def _peek_word(sql: str, i: int) -> tuple[str, int]:
    """The next word after position `i` (skipping whitespace),
    uppercased, and the offset just past it ("" when a non-word
    character comes first)."""
    while i < len(sql) and sql[i].isspace():
        i += 1
    j = i
    while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
        j += 1
    return sql[i:j].upper(), j


def statement_at(statements: list[Statement], offset: int) -> Statement | None:
    """The statement under `offset`: the first one that has not ended
    yet, so a cursor sitting right after a semicolon still picks the
    statement it terminates. Past the last statement, picks the last."""
    for statement in statements:
        if offset <= statement.end:
            return statement
    return statements[-1] if statements else None
