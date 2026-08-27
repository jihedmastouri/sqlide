"""Translation and locale-aware formatting.

One gettext domain ("sqlide"), one catalogue directory (sqlide/locale,
built from po/ by `make i18n`), and one entry point: install(), which
the application calls before it builds a single widget.

Modules mark their user-visible strings with the two functions this
module exports:

    from sqlide.i18n import _, ngettext

    label = _("Run query")
    status = ngettext("%d row", "%d rows", n) % n

`_` is a plain lookup and ngettext the plural one — never `if n == 1`,
because a language may have one plural form or six, and only the
catalogue knows which.

Language resolves, in this order:

1. `--language CODE` on the command line (take_language_argv);
2. the `language` key in settings.toml;
3. the system locale ($LANGUAGE / $LC_ALL / $LANG, or whatever
   setlocale(LC_ALL, "") settles on);
4. English, which is what the literals in the source already say.

A translation that is missing a string falls back to that source
literal, per string, so a partial catalogue is useful rather than
dangerous: gettext never returns blank.

Formatting goes through here too. `f"{n:,}"` is English — a French
reader expects 1 234 567 and a German one 1.234.567 — so numbers,
dates and byte sizes are rendered by format_number(), format_datetime()
and format_size() rather than spelled out at each call site.
"""

from __future__ import annotations

import gettext as _gettext
import locale
import os
from datetime import date, datetime
from pathlib import Path

#: The gettext domain, and the basename of every .po/.mo file.
DOMAIN = "sqlide"

#: Where compiled catalogues live: sqlide/locale/<lang>/LC_MESSAGES/.
LOCALE_DIR = Path(__file__).parent / "locale"

#: Languages the UI offers, English first. A code appears here once a
#: po/<code>.po exists; Preferences lists exactly this, so an entry
#: with no catalogue behind it would be a promise the app cannot keep.
#: Names are in the language itself — the way every language picker
#: worth using does it, so you can find yours without reading English.
LANGUAGES: dict[str, str] = {
    "en": "English",
    "fr": "Français",
}

#: What `language` means when it is left alone: follow the system.
SYSTEM = "system"

_translation: _gettext.NullTranslations = _gettext.NullTranslations()
_language = "en"


def _(message: str) -> str:
    """The marked string, translated if the catalogue has it."""
    return _translation.gettext(message)


def N_(message: str) -> str:
    """Mark a string for extraction without translating it yet.

    For a literal that has to exist before install() runs — a
    module-level table of labels, say. `_()` there would be looked up
    at import time, when the catalogue is not bound and the answer
    would be English forever. Mark with N_ where the string is
    written, and call `_()` on it where it is shown.
    """
    return message


def ngettext(singular: str, plural: str, n: int) -> str:
    """The plural form `n` calls for in the active language."""
    return _translation.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    """A translation disambiguated by context, for a word that is one
    string in English and two elsewhere ("Order" the noun, "Order" the
    verb)."""
    return _translation.pgettext(context, message)


def current_language() -> str:
    """The two-letter code actually in force."""
    return _language


def available_languages() -> dict[str, str]:
    """The shipped languages, as code -> name in that language. Only
    codes with a compiled catalogue (plus English, which needs none)."""
    shipped = {"en": LANGUAGES["en"]}
    for code, name in LANGUAGES.items():
        if code == "en":
            continue
        if (LOCALE_DIR / code / "LC_MESSAGES" / f"{DOMAIN}.mo").exists():
            shipped[code] = name
    return shipped


def system_language() -> str:
    """What the environment asks for, as a bare language code."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "").strip()
        if value and value not in ("C", "POSIX"):
            # LANGUAGE is a colon-separated preference list.
            return _normalise(value.split(":")[0])
    try:
        code, _enc = locale.getlocale(locale.LC_MESSAGES)
    except (AttributeError, ValueError):
        code = None
    return _normalise(code or "en")


def _normalise(value: str) -> str:
    """fr_FR.UTF-8@euro -> fr."""
    for sep in ("@", ".", "_", "-"):
        value = value.split(sep)[0]
    return value.lower() or "en"


def install(language: str | None = None) -> str:
    """Bind the domain and make `_` live. `language` is a code, or
    "system"/None to follow the environment. Returns the code that
    ended up in force, which is "en" whenever nothing else resolved —
    a language we do not ship is not an error, just untranslated.

    Also sets the process locale, so format_number() and friends group
    digits and name months the way that language does. A system with
    the locale unbuilt (common in containers) keeps C formatting; the
    translations still work, since gettext does not need the locale.
    """
    global _translation, _language

    requested = (language or SYSTEM).strip()
    code = system_language() if requested in ("", SYSTEM) else _normalise(
        requested
    )
    _translation = _gettext.translation(
        DOMAIN, localedir=str(LOCALE_DIR), languages=[code], fallback=True
    )
    _language = code if isinstance(_translation, _gettext.GNUTranslations) else "en"
    _set_locale(code)
    return _language


def _set_locale(code: str) -> None:
    """Best effort: try the language's usual locale names, and leave
    the process where it is when none of them are built."""
    candidates = ["" if code == system_language() else None]
    candidates += [f"{code}.UTF-8", code]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            locale.setlocale(locale.LC_ALL, candidate)
            return
        except locale.Error:
            continue


def take_language_argv(argv: list[str]) -> tuple[list[str], str | None]:
    """Pull `--language CODE` (or `--language=CODE`) out of argv,
    returning the rest and the code. Consumed before GTK sees the
    arguments, the way --config-dir is: it has to apply before any
    widget is built, and GTK would refuse an option it does not know.
    """
    rest: list[str] = []
    language: str | None = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--language" and index + 1 < len(argv):
            language = argv[index + 1]
            index += 2
            continue
        if item.startswith("--language="):
            language = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    return rest, language


# Formatting


def format_number(value: float, decimals: int = 0) -> str:
    """A number with the active locale's digit grouping and decimal
    mark: 1,234 in English, 1 234 in French."""
    try:
        return locale.format_string(f"%.{decimals}f", value, grouping=True)
    except (ValueError, TypeError):
        return f"{value:,.{decimals}f}"


def format_size(size: int | None, *, none: str = "—") -> str:
    """A byte count as a human-readable size, in locale digits. The
    unit suffixes are translatable: not every language writes kB."""
    if size is None:
        return none
    value = float(size)
    units = (
        _("B"),
        _("kB"),
        _("MB"),
        _("GB"),
        _("TB"),
    )
    for index, unit in enumerate(units):
        if value < 1024 or index == len(units) - 1:
            decimals = 0 if index == 0 else 1
            # "%(size)s %(unit)s" so a translator can reorder or drop
            # the space; some languages do not use one.
            return _("%(size)s %(unit)s") % {
                "size": format_number(value, decimals),
                "unit": unit,
            }
        value /= 1024
    return none


def format_datetime(moment: datetime) -> str:
    """A timestamp in the locale's own date and time order."""
    return _strftime(moment, "%c", "%Y-%m-%d %H:%M")


def format_date(day: date) -> str:
    """A date in the locale's own order: 2026-08-27 reads 27/08/2026
    in French and 8/27/2026 in the United States."""
    return _strftime(day, "%x", "%Y-%m-%d")


def format_time(moment: datetime) -> str:
    return _strftime(moment, "%X", "%H:%M:%S")


def _strftime(value: date, spec: str, fallback: str) -> str:
    """`spec` is one of the locale's own formats (%c, %x, %X). Under
    the C locale those degrade to fixed English forms, so we use the
    ISO-ish fallback there instead — an unset locale should not turn
    every timestamp into "Thu Aug 27"."""
    if _locale_is_c():
        return value.strftime(fallback)
    try:
        return value.strftime(spec)
    except ValueError:
        return value.strftime(fallback)


def _locale_is_c() -> bool:
    current = locale.setlocale(locale.LC_TIME)
    return current in ("C", "POSIX", "C.UTF-8")
