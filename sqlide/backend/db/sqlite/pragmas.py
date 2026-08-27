"""The PRAGMA catalog: which settings a SQLite connection has, what
each of them is, and what changing one costs (SQ-02).

SQLite has no `pg_settings` — `pragma_pragma_list` names the pragmas a
build supports but says nothing about their type, their default, or
whether setting one rewrites the file. So the knowledge lives here, as
a declaration: one `PragmaSpec` per pragma, with the type of control it
takes, its documented default, and the *class* of change applying it
makes. Nothing in this module imports a driver or GTK — it is a table
of facts the provider reads values into and the UI draws.

Three things the declaration exists to keep honest:

* **Scope.** Some pragmas last only as long as the connection
  (`foreign_keys`, `cache_size`), some are recorded in the file and
  outlive it (`journal_mode`, `auto_vacuum`, `user_version`), and some
  can only be set before the database is written to (`page_size`).
  A row says which, and the UI warns before applying anything that is
  not merely session state.
* **Read-only.** `page_count`, `freelist_count`, `compile_options`,
  `database_list` and `integrity_check` are questions, not settings.
  The check-style ones are work — `integrity_check` reads every page —
  so they are run on request rather than with the rest of the list.
* **Danger.** `writable_schema` lets a mistake corrupt the file beyond
  what SQL can express, so it is `advanced`: hidden until the user asks
  for the advanced set, and carrying its warning when shown. Nothing
  here is ever applied without the user pressing something.

Values are kept as strings throughout — that is what a PRAGMA answers
with and what the UI shows — and validated back into the pragma's own
vocabulary by `normalize()` before any statement is built.
"""

from __future__ import annotations

from dataclasses import dataclass

#: What kind of control a pragma takes.
BOOLEAN = "boolean"
ENUM = "enum"
INTEGER = "integer"
READONLY = "readonly"  # informational, listed with the rest
CHECK = "check"  # informational and expensive: run on request

#: When a change takes effect, and how long it lasts. The order is the
#: order of increasing consequence, which is also the order in which
#: the UI's warnings get louder.
SESSION = "session"  # this connection only, gone on close
PERSISTENT = "persistent"  # recorded in the file, outlives the connection
CONNECT = "connect"  # only settable before the database is used
REWRITE = "rewrite"  # rewrites the file, or its format


@dataclass(frozen=True)
class PragmaSpec:
    """One PRAGMA as the viewer knows it.

    `default` is SQLite's documented default, not this file's opinion —
    a value read back that differs from it is what makes a row worth
    noticing. `warning` is shown before the change is applied and is
    required for everything but a plain session setting.
    """

    name: str
    kind: str
    description: str
    default: str = ""
    scope: str = SESSION
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    warning: str = ""
    #: Kept off the list until the user turns the advanced set on. The
    #: pragmas that can corrupt a database rather than merely slow it
    #: down or make it less durable.
    advanced: bool = False

    @property
    def editable(self) -> bool:
        return self.kind in (BOOLEAN, ENUM, INTEGER)

    @property
    def needs_confirmation(self) -> bool:
        """Applying this is more than a session setting, so the user is
        told what it does before it happens."""
        return self.editable and self.scope != SESSION

    @property
    def scope_label(self) -> str:
        return _SCOPE_LABELS[self.scope]


_SCOPE_LABELS = {
    SESSION: "this connection only",
    PERSISTENT: "stored in the file",
    CONNECT: "applies at connect time",
    REWRITE: "rewrites the database file",
}

#: SQLite spells booleans back as 0/1 but accepts words on the way in.
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


@dataclass(frozen=True)
class PragmaState:
    """One pragma as it stands on a live connection.

    `value` is what the database answered a moment ago, never what the
    UI last wrote: every change re-reads (SQ-02), so a setting the
    engine ignored — `page_size` on a database that already has pages,
    `journal_mode` refused while a transaction is open — shows the
    value it really has rather than the one that was asked for.
    `error` is filled when the read itself failed, which is how a
    pragma a build does not have becomes a row saying so instead of a
    missing row.
    """

    spec: PragmaSpec
    value: str = ""
    error: str = ""

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def display_value(self) -> str:
        return display(self.spec, self.value)

    @property
    def display_default(self) -> str:
        return display(self.spec, self.spec.default)

    @property
    def is_default(self) -> bool:
        """Does this connection sit at SQLite's documented default?
        Only asked of settings — a page count is not a default."""
        if not self.spec.editable or not self.spec.default:
            return True
        try:
            return normalize(self.spec, self.value) == normalize(
                self.spec, self.spec.default
            )
        except PragmaError:
            return False


PRAGMAS: tuple[PragmaSpec, ...] = (
    # --- booleans -----------------------------------------------------
    PragmaSpec(
        name="foreign_keys",
        kind=BOOLEAN,
        default="0",
        description=(
            "Enforce foreign key constraints. Off by default for "
            "backwards compatibility; cannot be changed inside a "
            "transaction."
        ),
    ),
    PragmaSpec(
        name="recursive_triggers",
        kind=BOOLEAN,
        default="0",
        description="Let a trigger's own statements fire triggers again.",
    ),
    PragmaSpec(
        name="case_sensitive_like",
        kind=BOOLEAN,
        default="0",
        description=(
            "Make LIKE case-sensitive for ASCII characters. Changing it "
            "can invalidate the plans of LIKE-optimised indexes."
        ),
    ),
    PragmaSpec(
        name="ignore_check_constraints",
        kind=BOOLEAN,
        default="0",
        description="Stop enforcing CHECK constraints on this connection.",
    ),
    PragmaSpec(
        name="defer_foreign_keys",
        kind=BOOLEAN,
        default="0",
        description=(
            "Defer foreign key enforcement to the end of the "
            "transaction. Resets when the transaction ends."
        ),
    ),
    PragmaSpec(
        name="query_only",
        kind=BOOLEAN,
        default="0",
        description="Refuse every write on this connection.",
    ),
    PragmaSpec(
        name="legacy_alter_table",
        kind=BOOLEAN,
        default="0",
        description=(
            "Use the pre-3.25 ALTER TABLE RENAME, which does not fix up "
            "references to the renamed table."
        ),
    ),
    PragmaSpec(
        name="cell_size_check",
        kind=BOOLEAN,
        default="0",
        description=(
            "Check b-tree page integrity as pages are read: corruption "
            "is caught earlier, at a cost in speed."
        ),
    ),
    PragmaSpec(
        name="reverse_unordered_selects",
        kind=BOOLEAN,
        default="0",
        description=(
            "Return rows of an unordered SELECT in reverse: a way to "
            "find queries that rely on an order they never asked for."
        ),
    ),
    PragmaSpec(
        name="writable_schema",
        kind=BOOLEAN,
        default="0",
        advanced=True,
        scope=REWRITE,
        warning=(
            "Writable schema lets ordinary UPDATE statements rewrite "
            "sqlite_master. A mistake here corrupts the database in a "
            "way SQL cannot undo. Leave it off unless you are "
            "repairing a file you have a backup of."
        ),
        description=(
            "Allow writes to sqlite_master itself — a repair tool, not "
            "a setting."
        ),
    ),
    # --- enumerations -------------------------------------------------
    PragmaSpec(
        name="journal_mode",
        kind=ENUM,
        default="delete",
        scope=REWRITE,
        choices=("delete", "truncate", "persist", "memory", "wal", "off"),
        warning=(
            "Switching to or from WAL changes the file format: the "
            "database gets (or loses) its -wal and -shm files, needs "
            "SQLite 3.7 or newer to open, and cannot live on a network "
            "filesystem that lacks shared memory. `off` disables the "
            "rollback journal entirely — a crash mid-write then leaves "
            "a corrupt database."
        ),
        description=(
            "How transactions are journalled. WAL allows readers "
            "alongside a writer; the change is persistent."
        ),
    ),
    PragmaSpec(
        name="synchronous",
        kind=ENUM,
        default="2",
        scope=PERSISTENT,
        choices=("0", "1", "2", "3"),
        warning=(
            "Below FULL (2) a power failure or OS crash can leave the "
            "database corrupt — OFF (0) most of all. This is a "
            "durability trade, not a tuning knob."
        ),
        description=(
            "How hard SQLite makes the filesystem flush: "
            "0 OFF, 1 NORMAL, 2 FULL, 3 EXTRA."
        ),
    ),
    PragmaSpec(
        name="locking_mode",
        kind=ENUM,
        default="normal",
        choices=("normal", "exclusive"),
        scope=PERSISTENT,
        warning=(
            "Exclusive locking keeps the file locked until the "
            "connection closes: no other process can read or write it, "
            "and in WAL mode the mode cannot always be given back."
        ),
        description="Whether the file lock is released between transactions.",
    ),
    PragmaSpec(
        name="temp_store",
        kind=ENUM,
        default="0",
        choices=("0", "1", "2"),
        description=(
            "Where temporary tables and indexes live: "
            "0 default, 1 file, 2 memory."
        ),
    ),
    PragmaSpec(
        name="auto_vacuum",
        kind=ENUM,
        default="0",
        scope=REWRITE,
        choices=("0", "1", "2"),
        warning=(
            "Turning auto-vacuum on or off for a database that already "
            "has pages only takes effect after a VACUUM, which rewrites "
            "the entire file."
        ),
        description=(
            "Whether freed pages are returned to the filesystem: "
            "0 none, 1 full, 2 incremental."
        ),
    ),
    PragmaSpec(
        name="secure_delete",
        kind=ENUM,
        default="0",
        choices=("0", "1", "2"),
        description=(
            "Overwrite deleted content with zeroes: "
            "0 off, 1 on, 2 fast."
        ),
    ),
    # --- numbers ------------------------------------------------------
    PragmaSpec(
        name="cache_size",
        kind=INTEGER,
        default="-2000",
        description=(
            "Page cache size. A positive number counts pages, a "
            "negative one counts kibibytes (-2000 is 2 MiB)."
        ),
    ),
    PragmaSpec(
        name="busy_timeout",
        kind=INTEGER,
        default="0",
        minimum=0,
        description=(
            "Milliseconds to wait for a lock before giving up with "
            "SQLITE_BUSY. SQLite's own default is 0, but Python's "
            "sqlite3 sets 5000 on every connection it opens, so that "
            "is what a connection here starts at."
        ),
    ),
    PragmaSpec(
        name="page_size",
        kind=INTEGER,
        default="4096",
        scope=CONNECT,
        choices=("512", "1024", "2048", "4096", "8192", "16384", "32768", "65536"),
        warning=(
            "Page size can only be changed on a database with no "
            "tables yet, or by a VACUUM that rewrites the whole file. "
            "On an existing database this setting does nothing until "
            "you run VACUUM."
        ),
        description=(
            "Bytes per database page; a power of two between 512 and "
            "65536."
        ),
    ),
    PragmaSpec(
        name="mmap_size",
        kind=INTEGER,
        default="0",
        minimum=0,
        description=(
            "Bytes of the database to memory-map, capped by the "
            "build's limit. 0 disables mmap."
        ),
    ),
    PragmaSpec(
        name="wal_autocheckpoint",
        kind=INTEGER,
        default="1000",
        minimum=0,
        description=(
            "Pages the write-ahead log may grow to before it is "
            "checkpointed automatically."
        ),
    ),
    PragmaSpec(
        name="threads",
        kind=INTEGER,
        default="0",
        minimum=0,
        description="Auxiliary threads a single query may use for sorting.",
    ),
    PragmaSpec(
        name="user_version",
        kind=INTEGER,
        default="0",
        scope=PERSISTENT,
        warning=(
            "The user version is stored in the file header. Schema "
            "migration tools read it: changing it by hand can make one "
            "skip or repeat a migration."
        ),
        description=(
            "An integer the file carries for the application's own use "
            "— usually a schema version."
        ),
    ),
    # --- read-only ----------------------------------------------------
    PragmaSpec(
        name="page_count",
        kind=READONLY,
        description="Pages in the database file.",
    ),
    PragmaSpec(
        name="freelist_count",
        kind=READONLY,
        description=(
            "Unused pages the file is holding on to; VACUUM returns "
            "them to the filesystem."
        ),
    ),
    PragmaSpec(
        name="encoding",
        kind=READONLY,
        description=(
            "The text encoding the file was created with. Only "
            "changeable before anything is written."
        ),
    ),
    PragmaSpec(
        name="data_version",
        kind=READONLY,
        description=(
            "Changes when another connection has committed to the "
            "database — a cheap way to notice outside writes."
        ),
    ),
    PragmaSpec(
        name="max_page_count",
        kind=READONLY,
        description="The page limit the file is allowed to grow to.",
    ),
    # --- checks, run on request ---------------------------------------
    PragmaSpec(
        name="integrity_check",
        kind=CHECK,
        description=(
            "Read every page and report any corruption. Answers 'ok' "
            "on a healthy database; reads the whole file, so it costs "
            "as much as the database is big."
        ),
    ),
    PragmaSpec(
        name="quick_check",
        kind=CHECK,
        description=(
            "The integrity check without the expensive index-content "
            "comparisons."
        ),
    ),
    PragmaSpec(
        name="foreign_key_check",
        kind=CHECK,
        description=(
            "Report rows that violate a foreign key, whether or not "
            "enforcement is on."
        ),
    ),
    PragmaSpec(
        name="compile_options",
        kind=CHECK,
        description="The options this SQLite library was built with.",
    ),
    PragmaSpec(
        name="database_list",
        kind=CHECK,
        description=(
            "The databases this connection has open: the main file, "
            "temp, and anything ATTACHed."
        ),
    ),
)

_BY_NAME = {spec.name: spec for spec in PRAGMAS}


def spec(name: str) -> PragmaSpec | None:
    """The declaration for `name`, or None for a pragma this viewer
    does not know — which is how an unknown name from a config file is
    refused rather than executed."""
    return _BY_NAME.get((name or "").strip().lower())


def listed(advanced: bool = False) -> tuple[PragmaSpec, ...]:
    """The pragmas to show. The advanced ones — the ones that can
    corrupt a file rather than only slow it down — are left out until
    the user asks for them explicitly."""
    return tuple(s for s in PRAGMAS if advanced or not s.advanced)


def editable(advanced: bool = False) -> tuple[PragmaSpec, ...]:
    return tuple(s for s in listed(advanced) if s.editable)


class PragmaError(ValueError):
    """A value that is not one this pragma accepts."""


def normalize(target: PragmaSpec | str, value) -> str:
    """`value` in the pragma's own vocabulary, or PragmaError.

    Everything a pragma is set to passes through here, so a bad value
    from a text entry and a bad value from a hand-edited config file
    fail the same way — before any SQL is built.
    """
    found = target if isinstance(target, PragmaSpec) else spec(target)
    if found is None:
        raise PragmaError(f"Unknown pragma: {target}")
    if not found.editable:
        raise PragmaError(f"{found.name} is read-only")
    text = str(value).strip()
    if not text:
        raise PragmaError(f"{found.name} needs a value")
    if found.kind == BOOLEAN:
        lowered = text.lower()
        if lowered in _TRUE:
            return "1"
        if lowered in _FALSE:
            return "0"
        raise PragmaError(f"{found.name} takes on or off, not {text!r}")
    if found.kind == ENUM:
        lowered = text.lower()
        if lowered not in found.choices:
            allowed = ", ".join(found.choices)
            raise PragmaError(
                f"{found.name} takes one of {allowed}, not {text!r}"
            )
        return lowered
    try:
        number = int(text, 10)
    except ValueError:
        raise PragmaError(
            f"{found.name} takes a whole number, not {text!r}"
        ) from None
    if found.choices and str(number) not in found.choices:
        allowed = ", ".join(found.choices)
        raise PragmaError(f"{found.name} takes one of {allowed}, not {number}")
    if found.minimum is not None and number < found.minimum:
        raise PragmaError(f"{found.name} cannot be below {found.minimum}")
    if found.maximum is not None and number > found.maximum:
        raise PragmaError(f"{found.name} cannot be above {found.maximum}")
    return str(number)


def statement(target: PragmaSpec | str, value) -> str:
    """The `PRAGMA name = value` that applies `value`, with the value
    validated first — the name is one of ours and the value is a
    normalized literal, so nothing user-typed reaches the SQL text
    unchecked."""
    found = target if isinstance(target, PragmaSpec) else spec(target)
    if found is None:
        raise PragmaError(f"Unknown pragma: {target}")
    return f"PRAGMA {found.name} = {normalize(found, value)}"


def display(target: PragmaSpec | str, value: str) -> str:
    """A value as a person reads it: a boolean as on/off, an
    enumeration whose numbers have names spelled out, anything else as
    it came back."""
    found = target if isinstance(target, PragmaSpec) else spec(target)
    text = "" if value is None else str(value)
    if found is None or not text:
        return text
    if found.kind == BOOLEAN:
        return "on" if text.lower() in _TRUE else "off"
    named = _CHOICE_LABELS.get(found.name, {})
    return named.get(text.lower(), text)


#: The enumerations whose values are numbers with documented names.
_CHOICE_LABELS = {
    "synchronous": {
        "0": "0 · OFF", "1": "1 · NORMAL", "2": "2 · FULL", "3": "3 · EXTRA",
    },
    "temp_store": {"0": "0 · default", "1": "1 · file", "2": "2 · memory"},
    "auto_vacuum": {"0": "0 · none", "1": "1 · full", "2": "2 · incremental"},
    "secure_delete": {"0": "0 · off", "1": "1 · on", "2": "2 · fast"},
}


def choice_labels(target: PragmaSpec | str) -> tuple[tuple[str, str], ...]:
    """(value, label) for an enumeration's options, in declared order."""
    found = target if isinstance(target, PragmaSpec) else spec(target)
    if found is None:
        return ()
    return tuple((c, display(found, c)) for c in found.choices)


# --- connection defaults (CORE-13) ---------------------------------------
#
# Saved on the profile as a list of "name = value" strings rather than a
# TOML table: connections are an array of tables in connections.toml and
# the writer keeps config one level deep, so a nested table would be
# dropped on save. A list of strings round-trips, diffs a line at a
# time, and is readable by hand — which is the point of the file.


def parse_defaults(entries) -> list[tuple[PragmaSpec, str]]:
    """The saved defaults as (spec, value) pairs, skipping anything
    unknown or invalid. A hand-edited file with one bad line still
    applies the rest — and `default_errors` says what was skipped."""
    found = []
    for _entry, spec_value in _read_defaults(entries):
        if isinstance(spec_value, tuple):
            found.append(spec_value)
    return found


def default_errors(entries) -> list[str]:
    """One message per saved default that cannot be applied."""
    return [
        message
        for _entry, message in _read_defaults(entries)
        if isinstance(message, str)
    ]


def _read_defaults(entries):
    for entry in entries or ():
        text = str(entry).strip()
        if not text or text.startswith("#"):
            continue
        name, sep, raw = text.partition("=")
        if not sep:
            yield entry, f"{text!r} is not 'name = value'"
            continue
        found = spec(name)
        if found is None:
            yield entry, f"Unknown pragma: {name.strip()}"
            continue
        try:
            yield entry, (found, normalize(found, raw))
        except PragmaError as exc:
            yield entry, str(exc)


def format_defaults(values: dict) -> list[str]:
    """A {name: value} mapping as the lines the profile stores, in the
    catalog's order so the file does not churn."""
    lines = []
    for found in PRAGMAS:
        if found.name in values:
            lines.append(
                f"{found.name} = {normalize(found, values[found.name])}"
            )
    return lines
