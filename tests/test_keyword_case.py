"""The case completion inserts keywords in (CORE-48).

The setting is `sql_keyword_case` in settings.toml, with three modes,
and it is applied in exactly one place — backend.settings.
apply_keyword_case, which the completion controller runs over every
suggestion a provider marked as a keyword. Identifiers never go
through it: a table or column name keeps the case the catalog
reported, because a server may treat it as significant.
"""

from __future__ import annotations

import pytest

from sqlide.backend.settings import (
    DEFAULT_KEYWORD_CASE,
    KEYWORD_CASES,
    Settings,
    SettingsStore,
    apply_keyword_case,
    store,
)


@pytest.fixture()
def mode(monkeypatch):
    """Set the live setting without touching a file on disk."""

    def set_mode(value: str) -> None:
        monkeypatch.setattr(
            store, "settings", Settings(sql_keyword_case=value)
        )

    return set_mode


# The setting


def test_the_default_is_upper_case() -> None:
    assert DEFAULT_KEYWORD_CASE == "upper"
    assert Settings().sql_keyword_case == "upper"
    assert set(KEYWORD_CASES) == {"upper", "lower", "follow"}


def test_a_mode_on_disk_loads(tmp_path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('sql_keyword_case = "follow"\n', encoding="utf-8")
    assert SettingsStore(path).load().sql_keyword_case == "follow"


def test_an_unknown_mode_falls_back(tmp_path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(
        'sql_keyword_case = "Title"\ntheme = "dark"\n', encoding="utf-8"
    )
    settings = SettingsStore(path).load()
    assert settings.sql_keyword_case == DEFAULT_KEYWORD_CASE
    assert settings.theme == "dark"  # the rest of the file still applies


def test_the_mode_survives_a_restart(tmp_path) -> None:
    path = tmp_path / "settings.toml"
    saved = SettingsStore(path)
    saved.load()
    saved.update(sql_keyword_case="lower")
    assert SettingsStore(path).load().sql_keyword_case == "lower"


# The three modes


def test_upper_always_upper_cases(mode) -> None:
    mode("upper")
    assert apply_keyword_case("select", "sel") == "SELECT"
    assert apply_keyword_case("select", "SEL") == "SELECT"
    assert apply_keyword_case("select", "") == "SELECT"


def test_lower_always_lower_cases(mode) -> None:
    mode("lower")
    assert apply_keyword_case("select", "sel") == "select"
    assert apply_keyword_case("select", "SEL") == "select"
    assert apply_keyword_case("select", "") == "select"


def test_follow_takes_its_cue_from_the_prefix(mode) -> None:
    mode("follow")
    assert apply_keyword_case("select", "sel") == "select"
    assert apply_keyword_case("select", "SEL") == "SELECT"
    assert apply_keyword_case("select", "Sel") == "SELECT"


def test_follow_upper_cases_a_mixed_case_prefix(mode) -> None:
    mode("follow")
    assert apply_keyword_case("select", "sElect"[:4]) == "SELECT"
    assert apply_keyword_case("select", "sEl") == "SELECT"


def test_follow_with_nothing_typed_falls_back_to_the_default(mode) -> None:
    mode("follow")
    assert apply_keyword_case("select", "") == "SELECT"
    assert apply_keyword_case("select") == "SELECT"


# The provider and the controller


def test_the_keyword_provider_leaves_the_case_to_the_controller():
    completion = pytest.importorskip("sqlide.frontend.completion")
    provider = completion.KeywordCompletionProvider(["SELECT", "set"])
    items = provider.complete(
        completion.CompletionContext(text="SEL", offset=3, word="SEL")
    )
    assert [(c.text, c.detail) for c in items] == [("select", "keyword")]
    assert items[0].is_keyword


def test_an_identifier_suggestion_is_not_a_keyword():
    completion = pytest.importorskip("sqlide.frontend.completion")
    assert not completion.Completion("Orders", detail="table").is_keyword


@pytest.mark.parametrize(
    "setting, typed, expected",
    [
        ("upper", "sel", "SELECT"),
        ("lower", "SEL", "select"),
        ("follow", "sel", "select"),
        ("follow", "Sel", "SELECT"),
    ],
)
def test_the_controller_spells_keywords_as_asked(
    monkeypatch, mode, setting, typed, expected
):
    """The editor's own path: a provider's suggestions run through the
    controller, whose casing is read per request — so changing the
    setting takes effect on the next popup, with no restart."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("no display for GTK")
    from sqlide.frontend import completion as completion_module

    shown: list[list[completion_module.Completion]] = []
    monkeypatch.setattr(
        completion_module,
        "run_async",
        lambda work, done, _fail: done(work()),
    )

    view = Gtk.TextView()
    controller = completion_module.CompletionController(view)
    monkeypatch.setattr(controller, "_show", shown.append)
    controller.add_provider(
        completion_module.KeywordCompletionProvider(["select"])
    )
    controller.add_provider(_TableProvider())

    mode(setting)
    view.get_buffer().set_text(typed)
    assert [(c.text, c.detail) for c in shown[-1]] == [
        (expected, "keyword"),
        ("Selected_Items", "table"),  # the catalog's own case, untouched
    ]


class _TableProvider:
    """A catalog provider: its names are identifiers, not keywords."""

    def complete(self, context):
        from sqlide.frontend.completion import Completion

        return [Completion("Selected_Items", detail="table")]
