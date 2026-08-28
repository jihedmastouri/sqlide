"""Format SQL: keyword case, one clause per line, indented subqueries.

Deliberately modest, because a wrong reformat is worse than none. The
formatter re-spells a statement out of its lexical pieces — it never
rewrites an expression, never reorders anything and never invents or
drops a token. What it decides is case (keywords only) and where the
line breaks go:

- one clause per line: SELECT / FROM / WHERE / GROUP BY / HAVING /
  ORDER BY / LIMIT and their INSERT/UPDATE/DELETE counterparts;
- JOIN indented under its FROM, ON indented under its JOIN;
- AND / OR aligned under the WHERE (or HAVING) they belong to;
- items of a select/group/order/set list one per line, comma trailing
  or leading as the setting says;
- a parenthesised subquery opened on its own indented block, other
  parentheses left inline — `count(*)` is not a subquery;
- comments kept where they were: one that had a line to itself keeps
  it, one that trailed code still trails it.

Everything rests on `sql_split.lex()`, the scanner `split_statements`
already uses, so a keyword inside a string, a comment, a dollar-quoted
body or a quoted identifier is never mistaken for the real thing. No
GTK, no new dependency: the query console, the DDL preview and the
query builder all render their SQL through here, so one definition of
"how our SQL looks" exists.

A statement the scanner cannot read cleanly — an unterminated string
or comment, unbalanced parentheses — is returned exactly as it came
in, with a reason the caller can show. So is a routine body (CREATE
FUNCTION / PROCEDURE / TRIGGER), whose bare semicolons and procedural
statements are outside what this formatter claims to understand, and
so is any script carrying MySQL's `DELIMITER` client command, since
that changes what a semicolon means.

Keyword case is not a setting of its own: it is the same
`sql_keyword_case` completion already uses (CORE-48). "follow" has no
prefix to follow when a whole statement is reformatted, so it means
"leave every keyword as the author spelled it".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from sqlide.backend.sql_split import Piece, lex, split_statements

# Keyword case modes. "upper" and "lower" respell keywords; "leave"
# keeps whatever the author typed (what `sql_keyword_case = "follow"`
# resolves to here — see the module docstring).
CASES = ("upper", "lower", "leave")
DEFAULT_CASE = "upper"
DEFAULT_INDENT = 2


@dataclass(frozen=True)
class FormatOptions:
    keyword_case: str = DEFAULT_CASE
    indent: int = DEFAULT_INDENT
    comma_leading: bool = False
    dialect: str = ""


@dataclass(frozen=True)
class FormatResult:
    """`text` is always safe to put back in the editor: formatted when
    `reason` is empty, and the input verbatim when it is not."""

    text: str
    changed: bool
    reason: str = ""


# Keywords: what gets re-cased, and what may be followed by a space
# before "(" — a bare word before "(" is a function call, `count(*)`,
# and gets none. Function names are deliberately absent for that
# reason.
KEYWORDS = frozenset("""
ADD ALL ALTER AND ANY AS ASC BEGIN BETWEEN BY CASE CASCADE CHECK
COLLATE COLUMN COMMIT CONFLICT CONSTRAINT CREATE CROSS CURRENT DEFAULT
DEFERRABLE DELETE DESC DISTINCT DO DROP ELSE END ESCAPE EXCEPT EXISTS
EXPLAIN FALSE FETCH FILTER FIRST FOLLOWING FOR FOREIGN FROM FULL GROUP
GROUPS HAVING IF ILIKE IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY
LAST LATERAL LEFT LIKE LIMIT MATERIALIZED NATURAL NEXT NO NOT NOTHING
NULL NULLS OFFSET ON ONLY OR ORDER OUTER OVER PARTITION PRECEDING
PRIMARY RANGE RECURSIVE REFERENCES RENAME REPLACE RESTRICT RETURNING
RIGHT ROLLBACK ROW ROWS SAVEPOINT SELECT SET SIMILAR SOME TABLE TEMP
TEMPORARY THEN TIES TO TRANSACTION TRUE TRUNCATE UNBOUNDED UNION UNIQUE
UNLOGGED UPDATE USING VALUES VIEW WHEN WHERE WINDOW WITH WITHOUT
""".split())

# Clause heads, longest phrase first: each starts a fresh line at the
# indent of the block it is in.
_CLAUSES: tuple[tuple[str, ...], ...] = (
    ("INSERT", "INTO"), ("DELETE", "FROM"), ("ORDER", "BY"),
    ("GROUP", "BY"), ("UNION", "ALL"), ("ON", "CONFLICT"),
    ("SELECT",), ("FROM",), ("WHERE",), ("HAVING",), ("LIMIT",),
    ("OFFSET",), ("VALUES",), ("RETURNING",), ("UPDATE",), ("SET",),
    ("UNION",), ("EXCEPT",), ("INTERSECT",), ("WITH",), ("WINDOW",),
    ("FETCH",),
)
# JOIN phrases, one indent step in from their FROM.
_JOINS: tuple[tuple[str, ...], ...] = (
    ("LEFT", "OUTER", "JOIN"), ("RIGHT", "OUTER", "JOIN"),
    ("FULL", "OUTER", "JOIN"), ("NATURAL", "LEFT", "JOIN"),
    ("NATURAL", "RIGHT", "JOIN"), ("LEFT", "JOIN"), ("RIGHT", "JOIN"),
    ("FULL", "JOIN"), ("INNER", "JOIN"), ("CROSS", "JOIN"),
    ("NATURAL", "JOIN"), ("JOIN",),
)
# Clauses whose comma-separated items each get a line.
_LIST_CLAUSES = frozenset(
    {"SELECT", "GROUP BY", "ORDER BY", "SET", "RETURNING"}
)
# Clauses whose AND/OR continuation lines align one step in.
_BOOLEAN_CLAUSES = frozenset({"WHERE", "HAVING", "ON"})

# A `DELIMITER` line redefines what ends a statement; a script that
# uses one is left alone entirely.
_DELIMITER_RE = re.compile(r"^[ \t]*DELIMITER\b", re.IGNORECASE | re.MULTILINE)
_ROUTINE_WORDS = frozenset({"TRIGGER", "FUNCTION", "PROCEDURE"})


def format_sql(sql: str, options: FormatOptions | None = None) -> FormatResult:
    """Every statement in `sql`, formatted and joined back together.

    A statement that cannot be formatted is copied through verbatim
    and named in `reason`; the ones around it are still formatted.
    """
    options = options or FormatOptions()
    if _DELIMITER_RE.search(sql):
        return FormatResult(sql, False, "script uses DELIMITER")
    statements = split_statements(sql, options.dialect)
    if not statements:
        return FormatResult(sql, False, "")
    chunks: list[str] = []
    reasons: list[str] = []
    for statement in statements:
        result = format_statement(statement.text, options)
        if result.reason:
            reasons.append(result.reason)
        terminated = sql[statement.start : statement.end].rstrip().endswith(";")
        chunks.append(result.text + (";" if terminated else ""))
    tail = sql[statements[-1].end :].strip()
    if tail:  # a trailing comment is not a statement, but it is text
        chunks.append(tail)
    text = "\n\n".join(chunks)
    if not text.endswith("\n") and sql.endswith("\n"):
        text += "\n"
    return FormatResult(text, text != sql, "; ".join(dict.fromkeys(reasons)))


def format_statement(
    sql: str, options: FormatOptions | None = None
) -> FormatResult:
    """One statement, formatted. Returns it unchanged, with a reason,
    when it is not something this formatter will touch."""
    options = options or FormatOptions()
    text = sql.strip()
    if not text:
        return FormatResult(sql, False, "")
    pieces = lex(text, options.dialect)
    reason = _refuse(text, pieces)
    if reason:
        return FormatResult(sql, False, reason)
    formatted = _Writer(options).run(pieces)
    return FormatResult(formatted, formatted != sql, "")


def options_from_settings() -> FormatOptions:
    """The formatter options the user's settings.toml asks for. Kept
    here so the console, the DDL tab and the query builder cannot
    drift apart; imported lazily because this module stays pure."""
    from sqlide.backend.settings import store

    current = store.settings
    case = current.sql_keyword_case
    return FormatOptions(
        keyword_case=case if case in ("upper", "lower") else "leave",
        indent=max(1, current.sql_format_indent),
        comma_leading=current.sql_format_comma_leading,
    )


def _refuse(text: str, pieces: list[Piece]) -> str:
    """Why this statement is left alone, or "" to format it."""
    for piece in pieces:
        if not piece.closed:
            kind = {"string": "string", "comment": "comment"}.get(
                piece.kind, "quoted body"
            )
            return f"unterminated {kind}"
    depth = 0
    for piece in pieces:
        if piece.kind == "punct" and piece.text == "(":
            depth += 1
        elif piece.kind == "punct" and piece.text == ")":
            depth -= 1
            if depth < 0:
                return "unbalanced parentheses"
    if depth:
        return "unbalanced parentheses"
    words = [p.text.upper() for p in pieces if p.kind == "word"]
    if words[:1] == ["CREATE"] and _ROUTINE_WORDS & set(words[:6]):
        return "routine body"
    if "BEGIN" in words[:1]:
        return "block statement"
    return ""


@dataclass
class _Frame:
    """One block of clauses: the statement itself, or a subquery
    inside parentheses. `plain` counts the non-subquery parentheses
    open inside it — clause, comma and AND/OR breaks happen only at
    `plain == 0`, so a function call or an `IN (…)` list stays on one
    line."""

    indent: int
    clause: str = ""
    plain: int = 0
    between: bool = False  # a BETWEEN is open: its AND is not a break


class _Writer:
    """Turns a statement's pieces into lines. Pure text assembly: it
    reads no whitespace from the source except whether a comment had a
    line to itself."""

    def __init__(self, options: FormatOptions) -> None:
        self.options = options
        self.lines: list[str] = []
        self.line = ""  # the line being built, without its indent
        self.indent = 0
        self.prev: Piece | None = None  # last piece written on this line
        self.unary = False  # last piece was a sign, so no space after it

    def run(self, pieces: list[Piece]) -> str:
        self.stack = [_Frame(indent=0)]
        i = 0
        while i < len(pieces):
            i = self._step(pieces, i)
        self._flush()
        return "\n".join(self.lines)

    # Line building

    def _flush(self) -> None:
        if self.line:
            self.lines.append(" " * (self.indent * self.options.indent) + self.line)
        self.line = ""
        self.prev = None

    def _break(self, indent: int) -> None:
        self._flush()
        self.indent = indent

    def _write(self, piece: Piece, text: str) -> None:
        if self.line and self._space_before(piece, text):
            self.line += " "
        self.line += text
        self.prev = replace(piece, text=text)
        self.unary = text in ("-", "+") and self._is_unary()

    def _is_unary(self) -> bool:
        before = self.prev
        return before is None or (
            before.kind == "punct" and before.text not in (")",)
        ) or (before.kind == "word" and before.text.upper() in KEYWORDS)

    def _space_before(self, piece: Piece, text: str) -> bool:
        before = self.prev
        if before is None or self.unary:
            return False
        if before.text in ("(", ".", "::"):
            return False
        if text in (",", ";", ")", ".", "::"):
            return False
        if text == "(":
            # `count(` is a call, `IN (` is a keyword and a list, and
            # `INSERT INTO t (a, b)` is a column list, not a call.
            if self.stack[-1].clause in ("INSERT INTO", "VALUES"):
                return True
            return not (
                before.kind in ("word", "quoted")
                and before.text.upper() not in KEYWORDS
            )
        return True

    # Walking

    def _step(self, pieces: list[Piece], i: int) -> int:
        piece = pieces[i]
        frame = self.stack[-1]
        if piece.kind == "comment":
            return self._comment(piece, i)
        if piece.kind == "punct" and piece.text == "(":
            return self._open_paren(pieces, i)
        if piece.kind == "punct" and piece.text == ")":
            return self._close_paren(piece, i)
        if piece.kind == "punct" and piece.text == "," and frame.plain == 0:
            if frame.clause in _LIST_CLAUSES:
                return self._comma(piece, i)
        if piece.kind == "word" and frame.plain == 0:
            handled = self._maybe_clause(pieces, i)
            if handled is not None:
                return handled
        self._write(piece, self._spelling(piece))
        return i + 1

    def _spelling(self, piece: Piece) -> str:
        if piece.kind != "word" or self.options.keyword_case == "leave":
            return piece.text
        if piece.text.upper() not in KEYWORDS:
            return piece.text
        if self.prev is not None and self.prev.text == ".":
            return piece.text  # a column named "key" is not a keyword
        return (
            piece.text.upper()
            if self.options.keyword_case == "upper"
            else piece.text.lower()
        )

    def _phrase(self, pieces: list[Piece], i: int, phrases) -> tuple | None:
        """The longest phrase in `phrases` starting at `i`, or None."""
        for phrase in phrases:
            words = [
                p.text.upper()
                for p in pieces[i : i + len(phrase)]
                if p.kind == "word"
            ]
            if len(words) == len(phrase) and tuple(words) == phrase:
                return phrase
        return None

    def _maybe_clause(self, pieces: list[Piece], i: int) -> int | None:
        frame = self.stack[-1]
        word = pieces[i].text.upper()
        if phrase := self._phrase(pieces, i, _JOINS):
            frame.clause = "JOIN"
            self._break(frame.indent + 1)
            return self._emit_phrase(pieces, i, phrase)
        if word == "ON" and frame.clause == "JOIN":
            frame.clause = "ON"
            self._break(frame.indent + 2)
            self._write(pieces[i], self._spelling(pieces[i]))
            return i + 1
        if word == "BETWEEN":
            frame.between = True
        if word == "AND" and frame.between:
            frame.between = False  # `BETWEEN 1 AND 5` is one condition
            return None
        if word in ("AND", "OR") and frame.clause in _BOOLEAN_CLAUSES:
            self._break(
                frame.indent + (3 if frame.clause == "ON" else 1)
            )
            self._write(pieces[i], self._spelling(pieces[i]))
            return i + 1
        if phrase := self._phrase(pieces, i, _CLAUSES):
            frame.clause = " ".join(phrase)
            self._break(frame.indent)
            return self._emit_phrase(pieces, i, phrase)
        return None

    def _emit_phrase(self, pieces: list[Piece], i: int, phrase) -> int:
        """Write the `len(phrase)` words of a clause head, keeping any
        comment that sits between them."""
        written = 0
        while written < len(phrase):
            piece = pieces[i]
            if piece.kind == "comment":
                i = self._comment(piece, i)
                continue
            self._write(piece, self._spelling(piece))
            written += 1
            i += 1
        return i

    def _comma(self, piece: Piece, i: int) -> int:
        if self.options.comma_leading:
            self._break(self.stack[-1].indent + 1)
            self.line = ","
            self.prev = piece
            self.unary = False
        else:
            self._write(piece, ",")
            self._break(self.stack[-1].indent + 1)
        return i + 1

    def _comment(self, piece: Piece, i: int) -> int:
        own_line = piece.newline_before or not self.line
        if own_line:
            self._break(self.indent)
        self._write(piece, piece.text)
        if piece.text.startswith("--"):
            self._break(self.indent)  # never swallow what follows
        return i + 1

    def _open_paren(self, pieces: list[Piece], i: int) -> int:
        nxt = next(
            (p for p in pieces[i + 1 :] if p.kind != "comment"), None
        )
        subquery = nxt is not None and nxt.kind == "word" and nxt.text.upper() in (
            "SELECT", "WITH",
        )
        self._write(pieces[i], "(")
        if subquery:
            frame = _Frame(indent=self.indent + 1)
            self.stack.append(frame)
            self._break(frame.indent)
        else:
            self.stack[-1].plain += 1
        return i + 1

    def _close_paren(self, piece: Piece, i: int) -> int:
        frame = self.stack[-1]
        if frame.plain:
            frame.plain -= 1
            self._write(piece, ")")
        elif len(self.stack) > 1:
            self.stack.pop()
            self._break(frame.indent - 1)
            self._write(piece, ")")
        else:  # _refuse() rejects unbalanced input, so this is dead
            self._write(piece, ")")
        return i + 1
