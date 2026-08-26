"""Notes (CORE-09): the store behind the side panel's Notes page.

Notes are configuration-shaped content — Markdown bodies scoped to a
connection, a table or nothing — kept in notes.toml next to the rest of
the config (CORE-13), so the tests here are about the file being
readable, hand-editable and never losing a note whose target is gone.
"""

from __future__ import annotations

import tomllib

import pytest

from sqlide.backend import config, notes
from sqlide.backend.notes import Note, NotesStore


@pytest.fixture(autouse=True)
def clean_config(monkeypatch, tmp_path):
    config.set_config_dir(None)
    config.clear_errors()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    yield
    config.set_config_dir(None)
    config.clear_errors()


# Storage


def test_notes_live_in_notes_toml_in_the_config_directory(tmp_path):
    store = NotesStore()
    store.add("Retention", "# Heading\n\nKeep 90 days.")
    assert store.path == tmp_path / "xdg" / "sqlide" / "notes.toml"

    data = tomllib.loads(store.path.read_text(encoding="utf-8"))
    assert [n["title"] for n in data["note"]] == ["Retention"]
    assert data["note"][0]["body"].startswith("# Heading")
    assert data["note"][0]["scope"] == "global"


def test_a_note_written_by_hand_is_read_back(tmp_path):
    path = tmp_path / "notes.toml"
    path.write_text(
        "# notes I keep in git\n"
        "[[note]]\n"
        'id = "abc"\n'
        'title = "Deploys"\n'
        'body = "- one\\n- two"\n'
        'scope = "connection"\n'
        'connection = "prod"\n',
        encoding="utf-8",
    )
    store = NotesStore(path)
    (note,) = store.load()
    assert (note.id, note.title, note.scope) == ("abc", "Deploys", "connection")
    assert note.body == "- one\n- two"


def test_editing_a_note_keeps_the_comments_around_it(tmp_path):
    path = tmp_path / "notes.toml"
    store = NotesStore(path)
    note = store.add("Deploys", "before")
    path.write_text(
        "# hand-written preamble\n" + path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    store.update(note, body="after")
    text = path.read_text(encoding="utf-8")
    assert "# hand-written preamble" in text
    assert 'body = "after"' in text


def test_a_broken_notes_file_reports_and_falls_back(tmp_path):
    path = tmp_path / "notes.toml"
    path.write_text("[[note]\ntitle = ", encoding="utf-8")
    store = NotesStore(path)
    assert store.load() == []
    assert any(error.path == path for error in config.errors())


def test_a_note_with_an_unknown_scope_is_kept_as_a_general_note(tmp_path):
    path = tmp_path / "notes.toml"
    path.write_text(
        '[[note]]\ntitle = "T"\nscope = "galaxy"\n', encoding="utf-8"
    )
    store = NotesStore(path)
    (note,) = store.load()
    assert note.scope == notes.GLOBAL
    assert any("galaxy" in str(error) for error in config.errors())


def test_an_edit_on_disk_is_picked_up_by_the_watcher(tmp_path):
    path = tmp_path / "notes.toml"
    store = NotesStore(path)
    store.add("First", "body")
    seen: list[list[Note]] = []
    store.subscribe(seen.append)

    path.write_text(
        '[[note]]\nid = "x"\ntitle = "From disk"\n', encoding="utf-8"
    )
    config.watcher.poll()

    assert [n.title for n in store.notes] == ["From disk"]
    assert seen and [n.title for n in seen[-1]] == ["From disk"]
    config.watcher.unwatch(path)


def test_our_own_write_does_not_come_back_as_an_external_edit(tmp_path):
    path = tmp_path / "notes.toml"
    store = NotesStore(path)
    store.load()
    store.add("First", "body")
    assert config.watcher.poll() == []
    config.watcher.unwatch(path)


# Editing and deleting


def test_deleting_the_last_note_empties_the_file(tmp_path):
    path = tmp_path / "notes.toml"
    store = NotesStore(path)
    note = store.add("Only", "body")
    store.remove(note)
    assert store.load() == []
    assert "[[note]]" not in path.read_text(encoding="utf-8")


def test_updating_stamps_the_change_and_narrows_the_scope(tmp_path):
    store = NotesStore(tmp_path / "notes.toml")
    note = store.add(
        "T", "b", scope=notes.TABLE, connection="prod", table="orders"
    )
    store.update(note, scope=notes.CONNECTION)
    assert (note.connection, note.table) == ("prod", "")
    store.update(note, scope=notes.GLOBAL)
    assert (note.connection, note.table) == ("", "")
    assert note.updated >= note.created

    with pytest.raises(AttributeError):
        store.update(note, id="nope")


def test_an_empty_title_never_leaves_a_nameless_row(tmp_path):
    store = NotesStore(tmp_path / "notes.toml")
    assert store.add("   ", "body").title == "Untitled"


# Filtering


def _seeded(tmp_path) -> NotesStore:
    store = NotesStore(tmp_path / "notes.toml")
    store.add("General", "anything at all")
    store.add("Prod", "connection body", notes.CONNECTION, "prod")
    store.add("Orders", "table body", notes.TABLE, "prod", "orders")
    store.add("Users", "table body", notes.TABLE, "prod", "users")
    store.add("Staging", "other one", notes.CONNECTION, "staging")
    return store


def test_the_scope_filter_offers_all_this_connection_and_this_table(tmp_path):
    store = _seeded(tmp_path)
    assert len(store.filter(notes.GLOBAL)) == 5
    assert {n.title for n in store.filter(notes.CONNECTION, "prod")} == {
        "Prod",
        "Orders",
        "Users",
    }
    assert [
        n.title for n in store.filter(notes.TABLE, "prod", "orders")
    ] == ["Orders"]


def test_the_text_filter_searches_title_and_body(tmp_path):
    store = _seeded(tmp_path)
    assert [n.title for n in store.filter(text="ORDER")] == ["Orders"]
    assert {n.title for n in store.filter(text="table body")} == {
        "Orders",
        "Users",
    }
    assert store.filter(text="nothing here") == []


def test_the_list_is_newest_change_first(tmp_path):
    store = _seeded(tmp_path)
    for i, note in enumerate(store.notes):
        note.updated = f"2026-08-0{i + 1}T09:00:00"
    assert [n.title for n in store.filter()] == [
        "Staging",
        "Users",
        "Orders",
        "Prod",
        "General",
    ]
    store.update(store.notes[0], body="touched")  # the oldest, edited now
    assert store.filter()[0].title == "General"


# Orphans


def test_a_note_whose_target_is_gone_is_orphaned_not_dropped(tmp_path):
    store = _seeded(tmp_path)
    orders = next(n for n in store.notes if n.title == "Orders")
    prod = next(n for n in store.notes if n.title == "Prod")
    general = next(n for n in store.notes if n.title == "General")

    assert orders.is_orphaned(["prod"], ["orders", "users"]) is False
    assert orders.is_orphaned(["prod"], ["users"]) is True
    assert orders.is_orphaned(["staging"], None) is True
    assert prod.is_orphaned(["staging"]) is True
    assert general.is_orphaned([]) is False

    # Nothing known yet is not evidence of an orphan.
    assert orders.is_orphaned(None) is False

    # And it is still on file either way.
    assert len(NotesStore(store.path).load()) == 5


def test_the_scope_badge_names_the_object(tmp_path):
    store = _seeded(tmp_path)
    labels = {n.title: n.scope_label for n in store.notes}
    assert labels["General"] == "General"
    assert labels["Prod"] == "prod"
    assert labels["Orders"] == "orders"
