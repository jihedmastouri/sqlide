"""What a statement destroys, and how hard the app should make it.

Two halves, both pure:

- `classify()` says what one statement does — the action, the object it
  names, and whether a DELETE/UPDATE has a WHERE clause. It reuses the
  tokenizer in sql_split.py, so keywords inside strings, comments,
  dollar-quoted bodies and quoted identifiers are never mistaken for
  the real thing.
- `confirmation_level()` turns that, plus the connection's environment
  class and the user's setting, into one of "none" / "confirm" /
  "type" — the three rungs of the destructive-action ladder that
  frontend/confirm.py renders.

An unfiltered DELETE is the case that pays for all of this: it costs
one parse the read-only guard already does, and catching it once is
worth more than any amount of visual polish.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlide.backend.identity import UNSET
from sqlide.backend.sql_split import Token, tokens

# Object-type keywords that sit between the verb and the object name
# (DROP **TABLE IF EXISTS** orders), plus the qualifiers that can
# follow the verb (DELETE **FROM**, INSERT **INTO**).
_SKIPPED = frozenset(
    {
        "TABLE",
        "VIEW",
        "INDEX",
        "TRIGGER",
        "FUNCTION",
        "PROCEDURE",
        "ROUTINE",
        "DATABASE",
        "SCHEMA",
        "EVENT",
        "SEQUENCE",
        "MATERIALIZED",
        "TEMPORARY",
        "TEMP",
        "IF",
        "EXISTS",
        "NOT",
        "ONLY",
        "FROM",
        "INTO",
        "CONCURRENTLY",
        "UNIQUE",
        "OR",
        "REPLACE",
    }
)

_ACTIONS = {
    "DROP": "drop",
    "TRUNCATE": "truncate",
    "DELETE": "delete",
    "UPDATE": "update",
    "INSERT": "insert",
    "REPLACE": "insert",
    "ALTER": "alter",
    "CREATE": "create",
    "SELECT": "read",
    "WITH": "read",
    "EXPLAIN": "read",
    "SHOW": "read",
    "DESCRIBE": "read",
    "DESC": "read",
    "PRAGMA": "read",
}

# Actions that change or remove data or schema.
_DESTRUCTIVE = frozenset(
    {"drop", "truncate", "delete", "update", "insert", "alter"}
)
# The top rung: irreversible, or reaching every row at once.
_SEVERE = frozenset({"drop", "truncate"})

# The action verbs, spelled for a dialog.
_VERBS = {
    "drop": "DROP",
    "truncate": "TRUNCATE",
    "delete": "DELETE",
    "update": "UPDATE",
    "insert": "INSERT",
    "alter": "ALTER",
    "create": "CREATE",
}

LEVELS = ("none", "confirm", "type")

# ui.confirm_destructive
CONFIRM_MODES = ("always", "non-dev", "never")
DEFAULT_CONFIRM_MODE = "non-dev"


@dataclass(frozen=True)
class Risk:
    action: str  # see _ACTIONS values, or "other"
    target: str = ""  # the object the statement names, when it names one
    unfiltered: bool = False  # DELETE/UPDATE with no WHERE clause

    @property
    def destructive(self) -> bool:
        return self.action in _DESTRUCTIVE

    @property
    def severe(self) -> bool:
        """Worth the top rung: DROP and TRUNCATE, and a DELETE or
        UPDATE with no WHERE — one statement, every row."""
        return self.action in _SEVERE or (
            self.unfiltered and self.action in ("delete", "update")
        )

    def describe(self) -> str:
        """The action in a form a dialog can put in a sentence."""
        verb = _VERBS.get(self.action, self.action.upper())
        text = f"{verb} {self.target}".strip()
        if self.unfiltered:
            text += " (no WHERE clause — every row)"
        return text


def classify(sql: str) -> Risk:
    """What this single statement does. Unknown statements come back
    as "other", which is never treated as destructive: guessing wrong
    in that direction only adds dialogs nobody asked for."""
    found = [t for t in tokens(sql)]
    words = [t.word for t in found if not t.quoted]
    if not words:
        return Risk(action="other")
    action = _ACTIONS.get(words[0], "other")
    if action in ("read", "create", "other"):
        return Risk(action=action, target=_target_after(found, 1))
    unfiltered = action in ("delete", "update") and "WHERE" not in words
    return Risk(
        action=action,
        target=_target_after(found, 1),
        unfiltered=unfiltered,
    )


def worst(statements: list[str]) -> Risk:
    """The riskiest statement of a script — what a Run All has to be
    confirmed against."""
    risks = [classify(sql) for sql in statements]
    return max(
        risks,
        key=lambda r: (r.destructive, r.severe),
        default=Risk(action="other"),
    )


def confirmation_level(
    risk: Risk, environment: str = UNSET, mode: str = DEFAULT_CONFIRM_MODE
) -> str:
    """Which rung of the ladder this statement has to climb.

    "none" runs straight away, "confirm" opens a dialog naming the
    target, "type" additionally asks the user to type the object's
    name — so they have to look at what they are destroying.

    An unset environment is not development: a severe statement still
    asks, because the connection that was never classified is exactly
    the one where a mistake is unexpected.
    """
    if mode not in CONFIRM_MODES:
        mode = DEFAULT_CONFIRM_MODE
    if mode == "never" or not risk.destructive:
        return "none"
    if environment == "development":
        return "confirm" if mode == "always" else "none"
    if environment == "production":
        return "type" if risk.severe else "confirm"
    if environment == "staging":
        return "confirm"
    # Unset.
    if mode == "always":
        return "confirm"
    return "confirm" if risk.severe else "none"


def _target_after(found: list[Token], start: int) -> str:
    """The first token after `start` that is not a keyword we skip —
    the object the statement acts on. A quoted identifier wins even if
    it spells a keyword."""
    for token in found[start:]:
        if token.quoted:
            return token.text
        if token.word not in _SKIPPED:
            return token.text
    return ""
