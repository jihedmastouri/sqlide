"""Saved table shapes — the designer's templates (CORE-29).

A template is a `TableModel` (backend/db/table_model.py) somebody saved
under a name: the audit columns every table in a project carries, the
lookup-table pattern, the six columns that would otherwise be typed
again. It is user-level data, not workspace data — the same shape is
worth starting from on every connection — so templates live in the
config directory beside notes and saved SQL, one plain TOML file per
template in ``table_templates/``:

    version = 1
    name = "Audit columns"
    engine = "postgres"
    created = "2026-08-29T10:00:00"
    table_name = ""
    schema = ""

    [[column]]
    name = "created_at"
    type = "timestamptz"
    nullable = false

One file per template rather than one file of many, so a template can
be dropped in, mailed, or committed on its own, and a hand-written one
is a file you can see. The file is readable and editable by hand, like
every other config file (docs/configuration.md); a file that will not
parse is skipped, never raised, so one bad template cannot stop the
menu from listing the rest.

A template is *engine-tagged* but not engine-bound: it records the
engine it was saved on so opening it elsewhere can say what did not
survive, and the designer prunes the options that engine does not
offer (`table_model.prune_options`) and drops untranslatable types into
"Custom…" rather than rendering DDL a server would refuse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlide.backend import config, tomlwrite
from sqlide.backend.db.table_model import TableModel, from_dict, to_dict

#: Bumped when the file shape changes incompatibly. A template written
#: by a newer version is skipped rather than half-read.
TEMPLATE_VERSION = 1

DIRECTORY = "table_templates"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


@dataclass
class Template:
    """One saved shape: the model, the name it was saved under, and the
    engine it was saved on."""

    name: str
    model: TableModel
    engine: str = ""
    created: str = field(default_factory=_now)
    path: Path | None = None


def _inline(mapping: dict) -> str:
    """A TOML inline table — what a column's options map renders as.

    tomlwrite emits documents one table deep (a config file has no
    deeper shape); a template's columns are an array of tables that
    each carry an options map, which is exactly what an inline table is
    for, and tomllib reads one back without any help.
    """
    body = ", ".join(
        f"{tomlwrite.key(k)} = {tomlwrite.value(v)}"
        for k, v in mapping.items()
    )
    return "{" + body + "}" if body else "{}"


def _lines(mapping: dict) -> list[str]:
    return [
        (
            f"{tomlwrite.key(k)} = {_inline(v)}"
            if isinstance(v, dict)
            else f"{tomlwrite.key(k)} = {tomlwrite.value(v)}"
        )
        for k, v in mapping.items()
    ]


def dumps(template: Template) -> str:
    """`template` as the TOML text of its file."""
    data = to_dict(template.model)
    head = {
        "version": TEMPLATE_VERSION,
        "name": template.name,
        "engine": template.engine,
        "created": template.created,
        # `name` up there is the template's; this is the table's, kept
        # separate so a template can carry a suggested table name.
        "table_name": data["name"],
        "schema": data["schema"],
        "comment": data["comment"],
        "options": data["options"],
    }
    out = ["# sqlide table template — see docs/ddl.md"]
    out.extend(_lines(head))
    for section, key in (
        ("column", "columns"),
        ("constraint", "constraints"),
        ("index", "indexes"),
    ):
        for entry in data[key]:
            out.append("")
            out.append(f"[[{section}]]")
            out.extend(_lines(entry))
    return "\n".join(out) + "\n"


def from_data(data: dict, path: Path | None = None) -> Template | None:
    """One template off disk, or None when the file is not one.

    Deliberately unexcitable, for the same reason `load_state` is: a
    template is data about somebody else's database, and a file that
    cannot be read must cost that one entry in the menu, nothing more.
    """
    if not isinstance(data, dict) or not data:
        return None
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version < 1 or version > TEMPLATE_VERSION:
        return None
    name = str(data.get("name", "")).strip()
    if not name:
        return None
    model_data = {
        "name": str(data.get("table_name", "")),
        "schema": str(data.get("schema", "")),
        "comment": str(data.get("comment", "")),
        "options": data.get("options") or {},
        "columns": [c for c in data.get("column", []) if isinstance(c, dict)],
        "constraints": [
            c for c in data.get("constraint", []) if isinstance(c, dict)
        ],
        "indexes": [i for i in data.get("index", []) if isinstance(i, dict)],
    }
    try:
        model = from_dict(model_data)
    except (AttributeError, TypeError, ValueError):
        return None
    return Template(
        name=name,
        model=model,
        engine=str(data.get("engine", "")),
        created=str(data.get("created", "")) or _now(),
        path=path,
    )


def _slug(name: str) -> str:
    """A filename for a template name — readable, and safe on every
    platform the app runs on."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "template"


class TemplateStore:
    """The templates in the config directory.

    The directory is read on every listing rather than cached: it is a
    handful of small files, a template dropped in by hand (or by an
    agent) shows up without a restart, and there is no stale copy to
    keep in step with the disk.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        # Resolved late: the config directory can be redirected (CLI
        # flag, tests) after this module is imported.
        return self._directory or (config.config_dir() / DIRECTORY)

    def templates(self) -> list[Template]:
        """Every readable template, by name."""
        directory = self.directory
        try:
            paths = sorted(directory.glob("*.toml"))
        except OSError:
            return []
        found = [
            template
            for template in (
                from_data(config.load_toml(path), path) for path in paths
            )
            if template is not None
        ]
        return sorted(found, key=lambda t: t.name.lower())

    def find(self, name: str) -> Template | None:
        return next(
            (t for t in self.templates() if t.name == name),
            None,
        )

    def save(self, name: str, model: TableModel, engine: str = "") -> Template:
        """Write `model` under a unique name (an existing name gets
        " (2)" …, exactly as saved queries do)."""
        taken = {t.name for t in self.templates()}
        name = name.strip() or "Template"
        if name in taken:
            n = 2
            while f"{name} ({n})" in taken:
                n += 1
            name = f"{name} ({n})"
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_slug(name)}.toml"
        n = 2
        while path.exists():
            path = directory / f"{_slug(name)}-{n}.toml"
            n += 1
        template = Template(name=name, model=model, engine=engine, path=path)
        path.write_text(dumps(template), encoding="utf-8")
        return template

    def remove(self, template: Template) -> None:
        path = template.path or (self.directory / f"{_slug(template.name)}.toml")
        try:
            path.unlink()
        except OSError:
            pass


store = TemplateStore()
