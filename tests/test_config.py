"""File-system configuration: where it lives, how it is written, and
what happens when it is wrong.

The three halves of CORE-13, in order: backend/config.py (location,
error reporting, watching), backend/tomlwrite.py (writing TOML back
without losing comments), and the two stores that use them.
"""

from __future__ import annotations

import json

import pytest

from sqlide.backend import config, saved, tomlwrite
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.settings import Settings, SettingsStore
from sqlide.backend.workspaces import (
    HistoryEntry,
    TabState,
    Workspace,
    WorkspaceStore,
)


@pytest.fixture(autouse=True)
def clean_config(monkeypatch, tmp_path):
    """Every test gets its own config directory and a clean error log,
    and none of them touch the developer's real one."""
    config.set_config_dir(None)
    config.clear_errors()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    yield
    config.set_config_dir(None)
    config.clear_errors()


# Where config lives


def test_xdg_config_home_wins_over_the_platform_default(tmp_path):
    assert config.config_dir() == tmp_path / "xdg" / "sqlide"


def test_env_var_overrides_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_VAR, str(tmp_path / "env"))
    assert config.config_dir() == tmp_path / "env"


def test_cli_flag_overrides_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_VAR, str(tmp_path / "env"))
    config.set_config_dir(tmp_path / "flag")
    assert config.config_dir() == tmp_path / "flag"


def test_platform_default_when_nothing_is_set(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.os, "name", "posix")
    assert config.config_dir() == tmp_path / ".config" / "sqlide"
    monkeypatch.setattr(config.sys, "platform", "darwin")
    assert config.config_dir() == (
        tmp_path / "Library" / "Application Support" / "sqlide"
    )


def test_take_config_dir_argv_consumes_both_spellings(tmp_path):
    rest = config.take_config_dir_argv(
        ["sqlide", "--config-dir", str(tmp_path / "a"), "--gtk-debug"]
    )
    assert rest == ["sqlide", "--gtk-debug"]
    assert config.config_dir() == tmp_path / "a"

    rest = config.take_config_dir_argv(["sqlide", f"--config-dir={tmp_path}"])
    assert rest == ["sqlide"]
    assert config.config_dir() == tmp_path


def test_take_config_dir_argv_without_a_path_is_a_usage_error():
    with pytest.raises(SystemExit):
        config.take_config_dir_argv(["sqlide", "--config-dir"])


# Broken files


def test_broken_toml_reports_file_line_and_key(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('theme = "dark"\neditor_font_size = ??\n')
    assert config.load_toml(path) == {}
    (error,) = config.errors()
    assert error.path == path
    assert error.line == 2
    assert error.key == "editor_font_size"
    assert str(path) in str(error)


def test_missing_file_is_not_an_error(tmp_path):
    assert config.load_toml(tmp_path / "nope.toml") == {}
    assert config.errors() == []


def test_a_bad_value_falls_back_to_the_default_and_is_reported(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('theme = "chartreuse"\nvim_mode = true\n')
    store = SettingsStore(path)
    settings = store.load()
    assert settings.theme == "system"  # fell back
    assert settings.vim_mode is True  # the rest of the file still applies
    (error,) = config.errors()
    assert error.key == "theme" and error.line == 1


def test_a_broken_settings_file_loads_as_all_defaults(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("theme = [unclosed\n")
    settings = SettingsStore(path).load()
    assert settings == Settings()
    assert config.errors()


# Watching


def test_the_watcher_reports_a_changed_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("a = 1\n")
    seen = []
    watcher = config.FileWatcher()
    watcher.watch(path, seen.append)
    assert watcher.poll() == []  # nothing changed yet

    path.write_text("a = 2\n")
    assert watcher.poll() == [path]
    assert seen == [path]


def test_forget_keeps_our_own_write_from_looking_external(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("a = 1\n")
    watcher = config.FileWatcher()
    watcher.watch(path, lambda _p: None)
    path.write_text("a = 2\n")
    watcher.forget(path)
    assert watcher.poll() == []


def test_editing_settings_on_disk_reaches_subscribers(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('theme = "light"\n')
    store = SettingsStore(path)
    store.load()
    seen = []
    store.subscribe(lambda s: seen.append(s.theme))

    path.write_text('theme = "dark"\n')
    config.watcher.poll()
    assert store.settings.theme == "dark"
    assert seen == ["dark"]
    config.watcher.unwatch(path)


# tomlwrite


def test_dumps_and_reads_back_every_value_shape():
    data = {
        "text": 'a "quoted" \\ line',
        "flag": False,
        "number": 12,
        "list": ["a", "b"],
        "table": {"x": "1"},
        "connection": [{"name": "a"}, {"name": "b"}],
    }
    import tomllib

    assert tomllib.loads(tomlwrite.dumps(data)) == data


def test_merge_keeps_comments_and_key_order():
    text = (
        "# my settings\n"
        'theme = "light"   \n'
        "\n"
        "# how big the editor font is\n"
        "editor_font_size = 11\n"
    )
    out = tomlwrite.merge(text, {"editor_font_size": 14, "theme": "dark"})
    assert out == (
        "# my settings\n"
        'theme = "dark"\n'
        "\n"
        "# how big the editor font is\n"
        "editor_font_size = 14\n"
    )


def test_merge_appends_new_keys_and_tables_and_keeps_unknown_ones():
    text = 'theme = "dark"\nfrom_the_future = 1\n'
    out = tomlwrite.merge(text, {"theme": "dark", "vim_mode": True})
    assert "from_the_future = 1" in out
    assert "vim_mode = true" in out


def test_merge_of_an_empty_file_is_a_plain_dump():
    assert tomlwrite.merge("   \n", {"a": 1}) == tomlwrite.dumps({"a": 1})


# The settings store


def test_settings_written_by_the_ui_keep_the_file_readable(tmp_path):
    path = tmp_path / "settings.toml"
    store = SettingsStore(path)
    store.load()
    store.update(theme="dark", max_result_rows=10, keymap={"win.run": "<Control>r"})

    reloaded = SettingsStore(path).load()
    assert (reloaded.theme, reloaded.max_result_rows) == ("dark", 10)
    assert reloaded.keymap == {"win.run": "<Control>r"}
    assert config.errors() == []


def test_a_ui_change_preserves_a_hand_written_comment(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("# hands off\ntheme = \"light\"\n")
    store = SettingsStore(path)
    store.load()
    store.update(theme="dark")
    assert path.read_text().startswith("# hands off\ntheme = \"dark\"")


def test_a_settings_json_from_an_older_version_is_imported(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"theme": "dark", "editor_font_size": 13})
    )
    path = tmp_path / "settings.toml"
    settings = SettingsStore(path).load()
    assert (settings.theme, settings.editor_font_size) == ("dark", 13)
    assert path.exists()  # converted, so the import happens only once


# The workspace store


def _workspace() -> Workspace:
    workspace = Workspace(name="Work", color="blue")
    workspace.add_connection(
        ConnectionProfile(
            name="prod",
            kind="postgres",
            host="db.internal",
            port=5432,
            environment="production",
        )
    )
    workspace.tabs.append(TabState(kind="query", connection="prod", sql="SELECT 1"))
    workspace.add_history(
        HistoryEntry(sql="SELECT 1", connection="prod", timestamp="now")
    )
    return workspace


def test_a_workspace_is_three_files_split_by_what_changes(tmp_path):
    store = WorkspaceStore(tmp_path / "workspaces")
    workspace = _workspace()
    store.save(workspace)

    folder = store.path_for(workspace.id)
    assert sorted(p.name for p in folder.iterdir()) == [
        "connections.toml",
        "state.json",
        "workspace.toml",
    ]
    connections = (folder / "connections.toml").read_text()
    assert "[[connection]]" in connections and 'name = "prod"' in connections
    # Definitions only: no tab, no history, no query text in there.
    assert "SELECT 1" not in connections
    assert "SELECT 1" not in (folder / "workspace.toml").read_text()


def test_round_trip_export_edit_load(tmp_path):
    """Save, edit the file by hand the way a person or an agent would,
    load again: the edit is what the app sees, and nothing else moved."""
    store = WorkspaceStore(tmp_path / "workspaces")
    workspace = _workspace()
    store.save(workspace)
    folder = store.path_for(workspace.id)

    path = folder / "connections.toml"
    path.write_text(
        "# the production database\n"
        + path.read_text().replace('host = "db.internal"', 'host = "db.example"')
    )

    (loaded,) = WorkspaceStore(tmp_path / "workspaces").load()
    assert loaded.id == workspace.id
    assert loaded.name == "Work"
    assert loaded.color == "blue"
    assert loaded.connections[0].host == "db.example"
    assert loaded.connections[0].environment == "production"
    assert loaded.tabs[0].sql == "SELECT 1"
    assert loaded.history[0].sql == "SELECT 1"
    assert config.errors() == []

    # And saving it back does not throw away the comment.
    WorkspaceStore(tmp_path / "workspaces").save(loaded)
    assert path.read_text().startswith("# the production database")


def test_an_unknown_key_is_reported_and_the_workspace_still_opens(tmp_path):
    store = WorkspaceStore(tmp_path / "workspaces")
    workspace = _workspace()
    store.save(workspace)
    path = store.path_for(workspace.id) / "connections.toml"
    path.write_text(path.read_text() + 'favourite_colour = "green"\n')

    (loaded,) = WorkspaceStore(tmp_path / "workspaces").load()
    assert loaded.connections[0].name == "prod"
    assert any(error.key == "favourite_colour" for error in config.errors())


def test_a_workspace_json_from_an_older_version_is_converted(tmp_path):
    directory = tmp_path / "workspaces"
    directory.mkdir(parents=True)
    (directory / "abc.json").write_text(
        json.dumps(
            {
                "id": "abc",
                "name": "Old",
                "color": "green",
                "connections": [{"name": "db", "kind": "sqlite", "file_path": "x.db"}],
                "tabs": [{"kind": "table", "connection": "db", "table": "users"}],
                "selected_tab": 0,
            }
        )
    )
    (loaded,) = WorkspaceStore(directory).load()
    assert (loaded.id, loaded.name, loaded.color) == ("abc", "Old", "green")
    assert loaded.connections[0].file_path == "x.db"
    assert loaded.tabs[0].table == "users"
    assert (directory / "abc" / "connections.toml").exists()
    assert (directory / "abc.json.bak").exists()  # the original is kept
    assert not (directory / "abc.json").exists()


def test_stores_follow_the_config_dir_override(tmp_path):
    config.set_config_dir(tmp_path / "elsewhere")
    assert WorkspaceStore().directory == tmp_path / "elsewhere" / "workspaces"
    assert SettingsStore().path == tmp_path / "elsewhere" / "settings.toml"
    assert saved.SavedStore("snippets.json").path == (
        tmp_path / "elsewhere" / "snippets.json"
    )
