"""The PRAGMA viewer and editor (SQ-02).

Three layers, none of which needs a server: the catalog of
declarations (what a pragma is, what it costs, what values it takes),
the provider that reads and writes them on a live connection, and the
per-connection defaults a profile carries and the adapter applies on
every connect.

What is asserted beyond the plumbing is the ticket's safety rules —
a pragma that outlives the session says so and carries a warning, a
pragma that can corrupt a file is off the list until Advanced is asked
for, no value reaches SQL unvalidated, and a change is believed only
after it has been read back.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.db import registry
from sqlide.backend.db.base import ConnectorError
from sqlide.backend.db.sqlite import pragmas
from sqlide.backend.db.sqlite.connector import SqliteConnector


@pytest.fixture()
def sq(tmp_path):
    """A provider on a small database — populated, because several
    pragmas behave differently once a file has pages."""
    path = tmp_path / "sq02.db"
    sqlite3.connect(path).close()  # the adapter refuses missing files
    connector = SqliteConnector(str(path))
    connector.connect()
    connector.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    connector.execute("INSERT INTO t VALUES (1, 'one')")
    yield registry.create_provider("sqlite", connector), connector
    connector.close()


# The catalog


def test_every_kind_of_control_is_declared() -> None:
    """The ticket names one pragma of each kind; each is in the catalog
    with the control its type implies."""
    for name in ("foreign_keys", "recursive_triggers", "case_sensitive_like"):
        assert pragmas.spec(name).kind == pragmas.BOOLEAN
    for name in (
        "journal_mode", "synchronous", "locking_mode",
        "temp_store", "auto_vacuum",
    ):
        assert pragmas.spec(name).kind == pragmas.ENUM
        assert pragmas.spec(name).choices
    for name in ("cache_size", "busy_timeout", "page_size", "mmap_size"):
        assert pragmas.spec(name).kind == pragmas.INTEGER


def test_the_informational_pragmas_are_not_editable() -> None:
    for name in ("page_count", "freelist_count"):
        assert pragmas.spec(name).kind == pragmas.READONLY
        assert not pragmas.spec(name).editable
    for name in ("integrity_check", "compile_options", "database_list"):
        assert pragmas.spec(name).kind == pragmas.CHECK
        assert not pragmas.spec(name).editable


def test_anything_outliving_the_session_warns_first() -> None:
    """The rule the UI leans on: a change that is more than session
    state is announced, so no warning-free row can quietly rewrite a
    file (page_size, auto_vacuum, journal_mode among them)."""
    for spec in pragmas.PRAGMAS:
        if spec.editable and spec.scope != pragmas.SESSION:
            assert spec.needs_confirmation, spec.name
            assert spec.warning, spec.name
            assert spec.scope_label
    for name in ("page_size", "auto_vacuum", "journal_mode"):
        assert pragmas.spec(name).needs_confirmation


def test_the_dangerous_ones_are_hidden_until_asked_for() -> None:
    """writable_schema is a repair tool, not a setting: off the list
    until Advanced, and warning when it is on it."""
    listed = [spec.name for spec in pragmas.listed()]
    assert "writable_schema" not in listed
    advanced = [spec.name for spec in pragmas.listed(advanced=True)]
    assert "writable_schema" in advanced
    assert pragmas.spec("writable_schema").warning


def test_values_are_validated_into_the_pragmas_own_vocabulary() -> None:
    assert pragmas.normalize("foreign_keys", "on") == "1"
    assert pragmas.normalize("foreign_keys", "NO") == "0"
    assert pragmas.normalize("journal_mode", "WAL") == "wal"
    assert pragmas.normalize("busy_timeout", " 500 ") == "500"
    assert pragmas.statement("foreign_keys", True) == "PRAGMA foreign_keys = 1"


@pytest.mark.parametrize(
    "name, value",
    [
        ("foreign_keys", "maybe"),
        ("journal_mode", "sometimes"),
        ("busy_timeout", "soon"),
        ("busy_timeout", "-1"),  # below the declared minimum
        ("page_size", "3000"),  # not a power of two the file allows
        ("page_count", "5"),  # read-only
        ("nonsense", "1"),  # not a pragma this viewer knows
        ("foreign_keys", ""),
    ],
)
def test_a_value_a_pragma_does_not_take_never_becomes_sql(name, value) -> None:
    with pytest.raises(pragmas.PragmaError):
        pragmas.statement(name, value)


def test_values_are_shown_the_way_a_person_reads_them() -> None:
    assert pragmas.display("foreign_keys", "1") == "on"
    assert pragmas.display("synchronous", "2") == "2 · FULL"
    assert pragmas.display("cache_size", "-2000") == "-2000"
    assert pragmas.choice_labels("temp_store")[2] == ("2", "2 · memory")


# The provider


def test_the_list_covers_the_ticket_and_carries_descriptions(sq) -> None:
    provider, _connector = sq
    states = provider.list_pragmas()
    by_name = {state.name: state for state in states}
    assert "writable_schema" not in by_name  # advanced, not asked for
    for name in (
        "foreign_keys", "journal_mode", "cache_size",
        "page_count", "integrity_check",
    ):
        assert name in by_name
        assert by_name[name].spec.description
    # Name, current value, default and description — the four columns
    # the ticket asks a row for.
    assert by_name["foreign_keys"].value in ("0", "1")
    assert by_name["page_count"].value  # a populated file has pages
    assert by_name["cache_size"].spec.default == "-2000"


def test_the_checks_are_not_run_to_draw_the_list(sq) -> None:
    """integrity_check reads every page, so it is listed with an empty
    value and run on request."""
    provider, _connector = sq
    checks = [
        state for state in provider.list_pragmas()
        if state.spec.kind == pragmas.CHECK
    ]
    assert checks and all(state.value == "" for state in checks)
    result = provider.run_pragma_check("integrity_check")
    assert result.rows and result.rows[0][0] == "ok"
    assert provider.run_pragma_check("database_list").rows


def test_a_check_is_the_only_thing_run_that_way(sq) -> None:
    provider, _connector = sq
    with pytest.raises(ConnectorError):
        provider.run_pragma_check("foreign_keys")
    with pytest.raises(ConnectorError):
        provider.run_pragma_check("nonsense")


def test_applying_a_pragma_reads_the_value_back(sq) -> None:
    provider, connector = sq
    state = provider.set_pragma("foreign_keys", "on")
    assert state.value == "1"
    assert connector.execute("PRAGMA foreign_keys").rows[0][0] == 1
    assert provider.set_pragma("foreign_keys", "off").value == "0"


def test_a_change_the_database_ignored_shows_what_it_really_is(sq) -> None:
    """page_size on a populated file is silently ignored by SQLite.
    The row must show the size the file has, not the one asked for —
    the reason nothing here trusts its own write."""
    provider, _connector = sq
    before = provider.set_pragma("page_size", "4096").value
    after = provider.set_pragma("page_size", "16384").value
    assert after == before


def test_a_persistent_change_takes_and_is_read_back(sq) -> None:
    provider, _connector = sq
    assert provider.set_pragma("journal_mode", "wal").value == "wal"
    assert provider.set_pragma("journal_mode", "delete").value == "delete"


def test_the_provider_refuses_what_the_catalog_refuses(sq) -> None:
    provider, _connector = sq
    for name, value in (
        ("nonsense", "1"), ("page_count", "3"), ("busy_timeout", "later"),
    ):
        with pytest.raises(ConnectorError):
            provider.set_pragma(name, value)


def test_engines_without_pragmas_answer_with_nothing() -> None:
    """The capability, not the engine's name, is what the UI asks —
    and the engines that do not have the flag are safe to ask."""
    for kind in ("postgres", "mysql"):
        assert not registry.capabilities(kind).pragmas
    assert registry.capabilities("sqlite").pragmas


# Per-connection defaults (CORE-13)


def test_defaults_round_trip_through_the_profile_lines() -> None:
    lines = pragmas.format_defaults(
        {"busy_timeout": "5000", "foreign_keys": "on"}
    )
    # Catalog order, so the file does not churn between saves.
    assert lines == ["foreign_keys = 1", "busy_timeout = 5000"]
    parsed = [(spec.name, value) for spec, value in pragmas.parse_defaults(lines)]
    assert parsed == [("foreign_keys", "1"), ("busy_timeout", "5000")]
    assert pragmas.default_errors(lines) == []


def test_a_bad_default_costs_its_own_line_and_says_so() -> None:
    entries = [
        "# a note",
        "foreign_keys = 1",
        "nonsense = 1",
        "busy_timeout = soon",
        "no equals sign",
    ]
    assert [spec.name for spec, _v in pragmas.parse_defaults(entries)] == [
        "foreign_keys"
    ]
    assert len(pragmas.default_errors(entries)) == 3


def test_the_adapter_applies_the_defaults_on_connect(tmp_path) -> None:
    path = tmp_path / "defaults.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(
        str(path), pragmas=("foreign_keys = on", "busy_timeout = 4321")
    )
    connector.connect()
    try:
        assert connector.execute("PRAGMA foreign_keys").rows[0][0] == 1
        assert connector.execute("PRAGMA busy_timeout").rows[0][0] == 4321
        assert connector.pragma_errors == []
    finally:
        connector.close()


def test_a_hand_edited_default_does_not_stop_the_database_opening(
    tmp_path,
) -> None:
    path = tmp_path / "bad-defaults.db"
    sqlite3.connect(path).close()
    connector = SqliteConnector(
        str(path), pragmas=("nonsense = 1", "foreign_keys = on")
    )
    connector.connect()
    try:
        assert connector.execute("PRAGMA foreign_keys").rows[0][0] == 1
        assert len(connector.pragma_errors) == 1
        assert "nonsense" in connector.pragma_errors[0]
    finally:
        connector.close()


def test_the_profile_carries_its_defaults_to_the_adapter() -> None:
    profile = ConnectionProfile(
        name="local", kind="sqlite", file_path="/tmp/x.db",
        pragmas=["foreign_keys = 1"],
    )
    params = profile.connect_params()
    assert params["pragmas"] == ("foreign_keys = 1",)
    # A profile that never saved any is not made to carry an entry.
    assert ConnectionProfile(name="p", kind="sqlite").connect_params()[
        "pragmas"
    ] == ()


# The tab and the way in (GTK)


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    return Gtk


def _sidebar(connector, profile):
    from sqlide.frontend.sidebar import Sidebar

    def unused(*_args, **_kwargs):
        raise AssertionError("callback should not fire")

    bar = Sidebar(
        ensure_connector=lambda _profile: connector,
        on_open_table=unused,
        on_open_object=unused,
        on_open_section=unused,
        on_new_query=unused,
        on_open_cli=unused,
        on_open_definition=unused,
        on_edit_table=unused,
        on_open_function=unused,
        on_relation_graph=unused,
        on_view_indexes=unused,
        on_query_builder=unused,
        on_drop_object=unused,
        on_new_object=unused,
        on_mcp_server=unused,
        on_manage_users=unused,
        on_monitor=unused,
        on_open_schema=unused,
        on_edit_connection=unused,
        on_disconnect=unused,
        on_close_tabs=unused,
        count_tabs=lambda _name: 0,
        on_remove_connection=unused,
        on_add_connection=unused,
        show_error=unused,
    )
    bar.add_profile(profile)
    return bar


def _labels(menu) -> list[str]:
    out = []
    for index in range(menu.get_n_items()):
        value = menu.get_item_attribute_value(index, "label", None)
        if value is not None:
            out.append(value.get_string())
    return out


def test_a_file_connection_is_offered_its_pragmas(gtk, sq) -> None:
    _provider, connector = sq
    bar = _sidebar(connector, ConnectionProfile("notes", "sqlite"))
    assert "PRAGMAs…" in _labels(bar._menu_for(bar._roots.get_item(0)))


def test_an_engine_without_pragmas_is_not(gtk, sq) -> None:
    _provider, connector = sq
    bar = _sidebar(connector, ConnectionProfile("reports", "postgres"))
    assert "PRAGMAs…" not in _labels(bar._menu_for(bar._roots.get_item(0)))


def _tab(gtk, monkeypatch, connector, saved):
    """A PragmasTab whose reload is driven by hand: the real one runs on
    a worker thread, so the rows are fed in directly here."""
    from sqlide.frontend import pragmas_tab as module

    monkeypatch.setattr(module.PragmasTab, "reload", lambda self: None)
    return module.PragmasTab(
        ConnectionProfile("notes", "sqlite"),
        lambda _profile: connector,
        lambda message: None,
        lambda profile, lines: saved.append((profile.name, lines)),
    )


def _draw(tab, provider, advanced=False):
    states = provider.list_pragmas(advanced)
    tab._states = {state.name: state for state in states}
    tab._rebuild(states)
    return states


def test_each_kind_of_pragma_gets_the_control_it_needs(
    gtk, monkeypatch, sq
) -> None:
    from gi.repository import Adw

    provider, connector = sq
    tab = _tab(gtk, monkeypatch, connector, [])
    _draw(tab, provider)
    rows = {}
    for _group, row in tab._rows:
        title = getattr(row, "get_title", lambda: "")()
        rows[title] = row
    assert isinstance(rows["foreign_keys"], Adw.SwitchRow)
    assert isinstance(rows["journal_mode"], Adw.ComboRow)
    assert isinstance(rows["page_count"], Adw.ActionRow)
    # A numeric pragma's entry is wrapped with its note, so it is found
    # by the group it went into rather than by a title of its own.
    settings = tab._groups["settings"]
    assert any(group is settings for group, _row in tab._rows)


def test_the_read_only_rows_show_the_value_and_no_control(
    gtk, monkeypatch, sq
) -> None:
    provider, connector = sq
    tab = _tab(gtk, monkeypatch, connector, [])
    states = _draw(tab, provider)
    checks = [s for s in states if s.spec.kind == pragmas.CHECK]
    readonly = [s for s in states if s.spec.kind == pragmas.READONLY]
    assert checks and readonly
    for state in readonly + checks:
        assert not state.spec.editable


def test_save_as_defaults_stores_only_what_differs(
    gtk, monkeypatch, sq
) -> None:
    provider, connector = sq
    saved: list[tuple[str, list[str]]] = []
    tab = _tab(gtk, monkeypatch, connector, saved)
    _draw(tab, provider)
    # A file straight out of sqlite3 sits at SQLite's defaults, bar the
    # busy timeout: the Python driver sets 5000 on every connection it
    # opens, and the row reports what the connection actually has.
    assert tab._default_lines() == ["busy_timeout = 5000"]
    provider.set_pragma("foreign_keys", "on")
    provider.set_pragma("cache_size", "-8000")
    _draw(tab, provider)
    assert tab._default_lines() == [
        "foreign_keys = 1", "cache_size = -8000", "busy_timeout = 5000"
    ]
    tab._save_defaults()
    assert saved[-1][0] == "notes"
    assert "foreign_keys = 1" in saved[-1][1]


def test_the_advanced_pragmas_stay_off_the_page_until_asked_for(
    gtk, monkeypatch, sq
) -> None:
    provider, connector = sq
    tab = _tab(gtk, monkeypatch, connector, [])
    titles = lambda: [  # noqa: E731 - a two-line closure would read worse
        getattr(row, "get_title", lambda: "")() for _group, row in tab._rows
    ]
    _draw(tab, provider)
    assert "writable_schema" not in titles()
    _draw(tab, provider, advanced=True)
    assert "writable_schema" in titles()
