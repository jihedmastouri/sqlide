"""Writing TOML back out, keeping what a person wrote in the file.

Python ships a TOML reader (tomllib) and no writer, and the writers on
PyPI either drop comments or are a dependency we don't want for a few
hundred bytes of config. So: a small emitter for the value shapes
config actually uses — strings, booleans, numbers, arrays of strings,
tables of strings, and arrays of tables — plus merge(), which rewrites
an existing file's values in place.

What merge() preserves:

* every comment and blank line;
* the order of the keys already in the file, and of its tables;
* anything in the file the app doesn't know about (a key from a newer
  version, or a note someone left).

A table's body is regenerated as a unit — the comments above its
header survive, comments between its keys do not — because a table
here is a free-form map (keymap, lsp_defaults) whose keys come and go.
Top-level keys, which are the schema and the ones worth annotating,
keep their comments exactly.
"""

from __future__ import annotations

_BARE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def key(name: str) -> str:
    """A key rendered bare when it can be, quoted when it must be."""
    if name and set(name) <= _BARE:
        return name
    return value(str(name))


def value(item) -> str:
    """One TOML scalar or array."""
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, (int, float)):
        return repr(item)
    if isinstance(item, (list, tuple)):
        return "[" + ", ".join(value(i) for i in item) + "]"
    text = str(item)
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _is_table(item) -> bool:
    return isinstance(item, dict)


def _is_table_array(item) -> bool:
    return (
        isinstance(item, (list, tuple))
        and bool(item)
        and all(isinstance(i, dict) for i in item)
    )


def dumps(data: dict) -> str:
    """Render a whole document: scalars first, then tables and arrays
    of tables, since TOML puts every bare key before the first header."""
    lines: list[str] = []
    for name, item in data.items():
        if _is_table(item) or _is_table_array(item):
            continue
        lines.append(f"{key(name)} = {value(item)}")
    for name, item in data.items():
        if _is_table(item):
            lines.append("")
            lines.extend(_table_lines(name, item))
        elif _is_table_array(item):
            for entry in item:
                lines.append("")
                lines.extend(_table_lines(name, entry, array=True))
    return "\n".join(lines).strip("\n") + "\n"


def _table_lines(name: str, table: dict, *, array: bool = False) -> list[str]:
    header = f"[[{key(name)}]]" if array else f"[{key(name)}]"
    lines = [header]
    for k, v in table.items():
        if _is_table(v) or _is_table_array(v):
            continue  # config nests one level deep; deeper is a bug
        lines.append(f"{key(k)} = {value(v)}")
    return lines


def merge(text: str, data: dict) -> str:
    """`data` written over the document in `text`, keeping its comments
    and key order. Keys the file doesn't have are appended; keys the
    file has that `data` doesn't are left alone."""
    if not text.strip():
        return dumps(data)

    scalars = {
        k: v
        for k, v in data.items()
        if not _is_table(v) and not _is_table_array(v)
    }
    tables = {k: v for k, v in data.items() if _is_table(v)}
    table_arrays = {k: v for k, v in data.items() if _is_table_array(v)}

    head, sections = _split(text)
    out, written = _rewrite_head(head, scalars)
    for name, v in scalars.items():
        if name not in written:
            out.append(f"{key(name)} = {value(v)}")

    seen_tables: set[str] = set()
    for lead, name, _body in sections:
        if name in tables:
            seen_tables.add(name)
            out.extend(lead)
            out.extend(_table_lines(name, tables[name]))
        elif name in table_arrays:
            if name in seen_tables:
                continue  # every entry was emitted with the first one
            seen_tables.add(name)
            out.extend(lead)
            for entry in table_arrays[name]:
                out.extend(_table_lines(name, entry, array=True))
                out.append("")
            out.pop()
        # A table the app no longer knows about is dropped along with
        # its body: keeping half of a removed section is worse than
        # losing it, and the backup archive still has the old file.

    for name, table in tables.items():
        if name not in seen_tables:
            out.append("")
            out.extend(_table_lines(name, table))
    for name, entries in table_arrays.items():
        if name not in seen_tables:
            for entry in entries:
                out.append("")
                out.extend(_table_lines(name, entry, array=True))
    return "\n".join(out).strip("\n") + "\n"


def _split(text: str) -> tuple[list[str], list[tuple[list[str], str, list[str]]]]:
    """The document as (lines before the first header, sections), where
    a section is (its leading comment/blank lines, its name, its body)."""
    head: list[str] = []
    sections: list[tuple[list[str], str, list[str]]] = []
    pending: list[str] = []
    current: tuple[list[str], str, list[str]] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped.strip("[]").strip().strip('"').strip("'")
            if current is not None:
                sections.append(current)
            current = (pending, name, [])
            pending = []
            continue
        if current is None:
            if stripped.startswith("#") or not stripped:
                pending.append(line)
            else:
                head.extend(pending)
                pending = []
                head.append(line)
        else:
            current[2].append(line)
    if current is not None:
        sections.append(current)
    else:
        head.extend(pending)
    return head, sections


def _rewrite_head(head: list[str], scalars: dict) -> tuple[list[str], set[str]]:
    out: list[str] = []
    written: set[str] = set()
    for line in head:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip().strip('"').strip("'")
            if name in scalars:
                out.append(f"{key(name)} = {value(scalars[name])}")
                written.add(name)
                continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out, written
