"""Sidebar search: what matches, how tightly, and where.

Kept apart from frontend/sidebar.py — and free of any GTK import — so
the matching rules are testable without a display: the sidebar owns
the widgets, this owns the answer to "does this row match, and which
letters lit it up".

A query matches a name as a subsequence (the "usrs" → "users" trick),
contiguous hits ranking above scattered ones, and every matched
character comes back as a highlight range so the row can bold exactly
what the user typed.

Scopes narrow the search to object kinds. A scope set is a frozenset of
the keys in SCOPES; the empty set is "All", the default, and matches
every kind. Kinds the scope list doesn't name (triggers, events) are
searched under "All" only — the picker stays the short list of things
people actually scope by.

Beside the kinds the picker carries one opt-in, SYSTEM_SCOPE: without
it the server's own schemas and everything under them are not searched
at all (PG-03).
"""

from __future__ import annotations

# (key, label, node kinds) in picker order. "schemas" is listed for the
# engines that grow that level; on the others no row ever carries the
# kind and the scope simply matches nothing.
SCOPES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("connections", "Connections", ("connection",)),
    ("databases", "Databases", ("database",)),
    ("schemas", "Schemas", ("schema",)),
    ("tables", "Tables", ("table",)),
    ("views", "Views", ("view",)),
    ("indexes", "Indexes", ("index",)),
    ("functions", "Functions", ("function",)),
    ("columns", "Columns", ("column",)),
)

SCOPE_KEYS: tuple[str, ...] = tuple(key for key, _label, _kinds in SCOPES)

# Not a kind but an opt-in: the server's own schemas and everything
# under them are out of the hunt unless this is ticked, so a search for
# "users" finds the table and not the forty catalog views that mention
# one (PG-03). Kept out of SCOPES because it narrows nothing on its own
# — it widens what the other scopes are allowed to reach.
SYSTEM_SCOPE = "system"
SYSTEM_SCOPE_LABEL = "System schemas"

_KINDS_BY_SCOPE = {key: kinds for key, _label, kinds in SCOPES}

# Never a search hit of its own: placeholders and grouping rows —
# categories, and the property sections under a table — carry no object
# behind them.
_UNSEARCHABLE = ("note", "category", "section")


def scope_label(scopes: frozenset[str]) -> str:
    """What the Filter button says: "All", one scope's own name, or a
    count once several are ticked."""
    chosen = [
        label for key, label, _kinds in SCOPES if key in scopes
    ]
    if not chosen or len(chosen) == len(SCOPES):
        return "All"
    if len(chosen) == 1:
        return chosen[0]
    return f"{len(chosen)} kinds"


def in_scope(kind: str, scopes: frozenset[str], *, system: bool = False) -> bool:
    """Is a row of this kind searchable under the chosen scopes?

    `system` marks a row that belongs to a system schema — its own row
    or anything under it — which only the system opt-in reaches.
    """
    if kind in _UNSEARCHABLE:
        return False
    if system and SYSTEM_SCOPE not in scopes:
        return False
    scopes = scopes - {SYSTEM_SCOPE}
    if not scopes or len(scopes) == len(SCOPES):
        return True
    return any(kind in _KINDS_BY_SCOPE[key] for key in scopes)


def match(query: str, name: str) -> tuple[tuple[int, int, int], tuple[tuple[int, int], ...]] | None:
    """(sort key, highlight ranges) for a query against a row's name,
    or None when it doesn't match. Case-insensitive; ranges index the
    name as given, so they can be applied to the displayed text.

    The key sorts tighter, earlier, shorter matches first, so a
    contiguous "orders" beats an "o…r…d…e…r…s" scattered across a long
    name."""
    query = query.strip().lower()
    if not query:
        return None
    lowered = name.lower()
    start = lowered.find(query)
    if start != -1:  # contiguous: always beats scattered matches
        return (0, start, len(name)), ((start, start + len(query)),)
    position = 0
    hits: list[int] = []
    for char in query:
        position = lowered.find(char, position)
        if position == -1:
            return None
        hits.append(position)
        position += 1
    spread = position - hits[0] - len(query)
    return (1 + spread, hits[0], len(name)), _ranges(hits)


def _ranges(hits: list[int]) -> tuple[tuple[int, int], ...]:
    """Adjacent character hits merged into spans, so "us" in "users"
    bolds once rather than twice."""
    spans: list[list[int]] = []
    for index in hits:
        if spans and spans[-1][1] == index:
            spans[-1][1] = index + 1
        else:
            spans.append([index, index + 1])
    return tuple((start, end) for start, end in spans)


def highlight(text: str, ranges: tuple[tuple[int, int], ...]) -> str:
    """Pango markup for a row label with the matched letters bold."""
    from xml.sax.saxutils import escape

    if not ranges:
        return escape(text)
    out: list[str] = []
    cursor = 0
    for start, end in ranges:
        out.append(escape(text[cursor:start]))
        out.append("<b>" + escape(text[start:end]) + "</b>")
        cursor = end
    out.append(escape(text[cursor:]))
    return "".join(out)
