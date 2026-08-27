"""Translation and locale-aware formatting (CORE-46).

Three things are worth holding still here: that the shipped catalogue
really loads and translates (a build that forgets `make i18n` would
otherwise pass every other test), that a language resolves in the
documented order and falls back per string rather than blanking, and
that no new user-visible literal creeps into the frontend unmarked —
the last one is a source scan, so it needs no display.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime
from pathlib import Path

import pytest

from sqlide import i18n
from sqlide.backend.settings import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Settings,
    SettingsStore,
    configured_language,
)

FRONTEND = Path(__file__).resolve().parents[1] / "sqlide" / "frontend"


@pytest.fixture(autouse=True)
def _restore_language():
    """Every test here rebinds the domain; put English back after, so
    the order tests run in cannot matter."""
    yield
    i18n.install("en")


# The catalogue


def test_the_shipped_french_catalogue_loads_and_translates() -> None:
    assert i18n.install("fr") == "fr"
    assert i18n.current_language() == "fr"
    assert i18n._("Preferences") == "Préférences"
    assert i18n._("Cancel") == "Annuler"


def test_french_is_offered_because_it_has_a_compiled_catalogue() -> None:
    # available_languages() is what Preferences lists; a language with
    # no .mo behind it must not appear there.
    assert i18n.available_languages() == {"en": "English", "fr": "Français"}
    assert set(i18n.available_languages()) <= set(i18n.LANGUAGES)


def test_a_missing_string_falls_back_to_english_not_blank() -> None:
    i18n.install("fr")
    # The French catalogue is partial on purpose; anything it does not
    # carry has to come back as the source literal.
    assert i18n._("A string no catalogue will ever carry") == (
        "A string no catalogue will ever carry"
    )


def test_a_language_we_do_not_ship_is_english_not_an_error() -> None:
    assert i18n.install("qq") == "en"
    assert i18n._("Preferences") == "Preferences"


def test_plurals_come_from_the_catalogue() -> None:
    i18n.install("fr")
    assert i18n.ngettext("%s row", "%s rows", 1) % 1 == "1 ligne"
    assert i18n.ngettext("%s row", "%s rows", 4) % 4 == "4 lignes"
    i18n.install("en")
    assert i18n.ngettext("%s row", "%s rows", 1) % 1 == "1 row"
    assert i18n.ngettext("%s row", "%s rows", 4) % 4 == "4 rows"


def test_the_marker_that_does_not_translate_returns_its_argument() -> None:
    i18n.install("fr")
    assert i18n.N_("Light") == "Light"
    assert i18n._(i18n.N_("Cancel")) == "Annuler"


# Where the language comes from


def test_the_flag_is_taken_out_of_argv_in_both_spellings() -> None:
    assert i18n.take_language_argv(["sqlide", "--language", "fr"]) == (
        ["sqlide"],
        "fr",
    )
    assert i18n.take_language_argv(["sqlide", "--language=fr"]) == (
        ["sqlide"],
        "fr",
    )
    # Everything else is left for GTK.
    assert i18n.take_language_argv(["sqlide", "--gapplication-service"]) == (
        ["sqlide", "--gapplication-service"],
        None,
    )


def test_the_system_locale_is_read_when_nothing_asks_for_a_language(
    monkeypatch,
) -> None:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANGUAGE", "fr_FR.UTF-8:en")
    assert i18n.system_language() == "fr"
    assert i18n.install("system") == "fr"
    assert i18n._("Cancel") == "Annuler"


def test_an_unset_environment_lands_on_english(monkeypatch) -> None:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.setenv(var, "C")
    assert i18n.system_language() == "en"


# The setting


def test_language_is_a_setting_with_system_as_its_default() -> None:
    assert Settings().language == DEFAULT_LANGUAGE == i18n.SYSTEM
    assert "fr" in LANGUAGES


def test_a_language_on_file_is_read_back(tmp_path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('language = "fr"\n', encoding="utf-8")
    assert SettingsStore(path).load().language == "fr"


def test_a_language_we_do_not_ship_falls_back_to_the_default(
    tmp_path,
) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('language = "kl"\n', encoding="utf-8")
    assert SettingsStore(path).load().language == DEFAULT_LANGUAGE


def test_the_setting_is_read_without_loading_the_store(
    tmp_path, monkeypatch
) -> None:
    """main() needs the language before the app exists — earlier than
    the store's own load, which would subscribe the file watcher."""
    path = tmp_path / "settings.toml"
    path.write_text('language = "fr"\n', encoding="utf-8")
    monkeypatch.setattr("sqlide.backend.settings.store.path", path)
    assert configured_language() == "fr"
    path.write_text("not toml at all [[[\n", encoding="utf-8")
    assert configured_language() == DEFAULT_LANGUAGE


# Formatting


def test_sizes_are_formatted_with_translatable_units() -> None:
    assert i18n.format_size(None) == "—"
    assert i18n.format_size(512) == "512 B"
    assert i18n.format_size(1536) == "1.5 kB"
    assert i18n.format_size(5 * 1024**4) == "5.0 TB"
    i18n.install("fr")
    assert i18n.format_size(1536) == "1.5 ko"


def test_numbers_and_dates_do_not_hard_code_english() -> None:
    # The exact separators depend on which locales the machine has
    # built, so what is asserted is that the helpers are used at all
    # and stay parseable — not a particular grouping character.
    assert i18n.format_number(1234567).replace(",", "") == "1234567"
    assert i18n.format_number(1.25, decimals=2)[-2:] == "25"
    moment = datetime(2026, 8, 27, 14, 5, 30)
    for rendered in (
        i18n.format_datetime(moment),
        i18n.format_date(moment.date()),
        i18n.format_time(moment),
    ):
        assert rendered and "%" not in rendered


# The source itself


def _sources() -> list[Path]:
    return sorted(FRONTEND.glob("*.py"))


def test_every_frontend_module_that_shows_a_string_marks_it() -> None:
    """A sampled set of widget properties — the ones that put words on
    screen — must not carry a bare literal. This is the guard the
    ticket asks for: a new `label="Run"` fails here rather than
    shipping untranslatable."""
    string = r'"(?:[^"\\]|\\.)*"'
    group = rf"{string}(?:\s*{string})*"
    patterns = (
        re.compile(
            r"\b(?:label|title|subtitle|heading|body|tooltip_text"
            rf"|placeholder_text)=({group})"
        ),
        re.compile(
            r"\.(?:set_label|set_title|set_subtitle|set_tooltip_text"
            rf"|set_placeholder_text|set_description)\(({group})\)"
        ),
        re.compile(rf"add_response\(\s*{string},\s*({group})"),
    )
    # Literals that are not prose: a SQL keyword, a column marker and
    # the like keep the spelling the server uses.
    allowed = {'"NULL"', '"ON"', '"%"', '"localhost"', '"100%"', '";\\n"'}
    offenders = []
    for path in _sources():
        source = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(source):
                literal = match.group(1)
                if literal in allowed:
                    continue
                body = "".join(re.findall(string, literal)).replace('"', "")
                if not re.search(r"[A-Za-z]", body):
                    continue
                if re.fullmatch(r"[a-z0-9_-]+", body) or "-symbolic" in body:
                    continue
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: {literal}")
    assert not offenders, "unmarked user-visible strings:\n" + "\n".join(
        offenders
    )


def test_no_module_translates_at_import_time() -> None:
    """`_()` at module level is looked up before install() binds the
    catalogue, so it would be English forever. Such a string is marked
    with N_ and translated where it is shown."""
    early = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bodies = [
            statement
            for statement in tree.body
            if not isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
        ]
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bodies += [
                    statement
                    for statement in node.body
                    if not isinstance(
                        statement, (ast.FunctionDef, ast.AsyncFunctionDef)
                    )
                ]
        for statement in bodies:
            for node in ast.walk(statement):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("_", "ngettext")
                ):
                    early.append(f"{path.name}:{node.lineno}")
    assert not early, "translated at import time: " + ", ".join(early)


def test_plurals_are_never_built_with_a_conditional_s() -> None:
    """`f"{n} row{'' if n == 1 else 's'}"` cannot be translated into a
    language with a different plural rule; ngettext can."""
    pattern = re.compile(r"if \w+ == 1 else ['\"]s['\"]|\bs['\"] if ")
    offenders = [
        f"{path.name}:{source[: m.start()].count(chr(10)) + 1}"
        for path in _sources()
        for source in [path.read_text(encoding="utf-8")]
        for m in pattern.finditer(source)
    ]
    assert not offenders, "hand-built plurals: " + ", ".join(offenders)
