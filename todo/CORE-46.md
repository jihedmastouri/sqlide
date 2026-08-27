## CORE-46 — Multi-language support (i18n)

- **Status:** done — French (`fr`) ships end to end: 268 of 457 strings
  (menus, tabs, buttons, dialogs, the settings pages and every plural
  form), partial and unreviewed, and marked as such in `po/fr.po` and
  `docs/configuration.md`. The rest fall back to English per string.
- **Depends on:** CORE-13

### Problem

Every string in the app is a hard-coded English literal. There is no way to ship
another language, and no way for a contributor to supply one.

### Goal

The UI can be translated, and the language is picked up from the system locale or
set explicitly in configuration.

### Approach

Use gettext (the native choice for a GTK app): mark strings with `_()`, extract a
`.pot`, keep `.po`/`.mo` files under a `po/` directory, and wire the domain up at
startup. Add a `language` key to the TOML config (CORE-13) that overrides the
system locale, plus a Preferences control listing the languages actually shipped.

Cover the mechanics, not just the strings: number, date and byte-size formatting
should go through locale-aware helpers rather than `f"{x:,}"`, and any string
built by concatenation needs to become a single format string so translators can
reorder it.

Ship at least one non-English translation end to end so the pipeline is proven,
even if it is partial.

### Acceptance criteria

- [x] User-visible strings in `sqlide/frontend/` are marked for translation;
      a `make` target regenerates the `.pot` and compiles `.mo` files.
      (`make i18n`: xgettext -> msgmerge -> msgfmt into `sqlide/locale/`.)
- [x] Language resolves from, in order: CLI flag, config key, system locale,
      English fallback. A missing translation falls back per-string, never blank.
      (`sqlide --language fr`, the `language` key, `$LANGUAGE`/`$LANG`.)
- [x] At least one non-English locale is shipped and switching to it visibly
      changes the UI.
- [x] Plurals use `ngettext`, not `if n == 1`. A test fails a hand-built one.
- [x] Dates, numbers and sizes render per locale, through the helpers in
      `sqlide/i18n.py`; `backend/db/metrics.format_bytes` folded into
      `i18n.format_size` so there is one byte formatter.
- [x] A test asserts no untranslated literal creeps into a sampled set of widgets,
      or at minimum that the catalogue loads and a known string translates.
      (`tests/test_i18n.py` does both.)
- [x] `docs/` explains how to add a language.
      (`docs/configuration.md` -> Languages.)

### Out of scope

Right-to-left layout mirroring, and translating the docs themselves.
