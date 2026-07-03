"""Completion provider backed by a language server.

Bridges the editor's CompletionProvider interface to the servers
managed in sqlide.lsp.servers. set_profile() and set_choice() are
called on the main thread when the console's connection/database/LSP
dropdowns change; complete() runs on a worker thread and may block on
server startup or I/O. LSP failures degrade to no suggestions — the
keyword provider still runs alongside.

Application settings are consulted per request: the master switch
(lsp_enabled) silences everything, and a console left on "auto" first
resolves through the per-kind default in settings.lsp_defaults before
falling back to the built-in resolution in sqlide.lsp.servers.
"""

from __future__ import annotations

import re

from sqlide.backend import settings as app_settings
from sqlide.backend.connections import ConnectionProfile
from sqlide.frontend.completion import (
    Completion,
    CompletionContext,
    CompletionProvider,
)
from sqlide.lsp import servers

# ${1:placeholder} -> placeholder, $1 -> "" (snippet-format inserts).
_SNIPPET_RE = re.compile(r"\$\{\d+:?([^}]*)\}|\$\d+")

_ITEM_KINDS = {
    1: "text", 2: "method", 3: "function", 4: "constructor", 5: "field",
    6: "variable", 7: "class", 8: "interface", 9: "module", 10: "property",
    11: "unit", 12: "value", 13: "enum", 14: "keyword", 15: "snippet",
    16: "color", 17: "file", 18: "reference", 19: "folder",
    20: "enum member", 21: "constant", 22: "struct", 23: "event",
    24: "operator", 25: "type parameter",
}


class LspCompletionProvider(CompletionProvider):
    def __init__(self) -> None:
        self._profile: ConnectionProfile | None = None
        self._choice = servers.AUTO

    def set_profile(self, profile: ConnectionProfile | None) -> None:
        self._profile = profile

    def set_choice(self, choice: str) -> None:
        """servers.AUTO, servers.NONE, or an available_servers() name."""
        self._choice = choice

    def complete(self, context: CompletionContext) -> list[Completion]:
        profile, choice = self._profile, self._choice
        if profile is None or choice == servers.NONE:
            return []
        config = app_settings.store.settings
        if not config.lsp_enabled:
            return []
        if choice == servers.AUTO:
            choice = config.lsp_defaults.get(profile.kind, servers.AUTO)
            if choice == servers.NONE:
                return []
        server = servers.manager.server_for(profile, choice)
        if server is None:
            return []
        try:
            items = server.completions(context.text, context.offset)
        except Exception:
            return []
        prefix = context.word.lower()
        results = []
        for item in sorted(
            items, key=lambda i: i.get("sortText") or i.get("label") or ""
        ):
            text = item.get("insertText") or item.get("label") or ""
            if item.get("insertTextFormat") == 2:
                text = _SNIPPET_RE.sub(lambda m: m.group(1) or "", text)
            # Acceptance replaces the current word, so only offer items
            # that extend it (servers like sqls return everything).
            if not text.lower().startswith(prefix) or text == context.word:
                continue
            results.append(
                Completion(text, detail=_ITEM_KINDS.get(item.get("kind"), ""))
            )
        return results
