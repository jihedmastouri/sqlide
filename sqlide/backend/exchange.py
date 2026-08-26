"""Workspaces and connections as portable XML files.

The workspace store's own files are TOML keyed by a machine-local id,
with passwords living in that machine's keyring — good for the app,
useless for carrying a setup to another installation. This module is
the transfer format instead: one readable, hand-editable XML file per
workspace, with everything that describes *how to reach a database*
and nothing that only makes sense on the machine it came from (ids,
open tabs, query history).

    <sqlide format="1" exported="2026-08-06T10:00:00">
      <workspace name="Work" color="blue">
        <connection name="reports" kind="postgres" environment="production">
          <host>db.example.com</host>
          <port>5432</port>
          <user>app</user>
          <database>reports</database>
        </connection>
        <filters table="reports.reports.orders" name="open">…</filters>
      </workspace>
    </sqlide>

Rules the format follows, so files stay small and keep working across
versions:

- Only fields that differ from their default are written; a reader
  fills the rest in from ConnectionProfile's own defaults.
- Elements a reader does not know are skipped rather than fatal, so a
  file from a newer version still imports what it can.
- Passwords are left out unless the export explicitly asks for them
  (they are secrets, and these files get mailed around). Importing a
  connection without one simply asks for it on first connect.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from sqlide.backend import identity
from sqlide.backend.connections import ConnectionProfile
from sqlide.backend.workspaces import Workspace

FORMAT = "1"
ROOT_TAG = "sqlide"

# Never written unless the caller opts in (see workspace_to_xml).
SECRET_FIELDS = ("password", "ssh_password")


class ExchangeError(Exception):
    """The file is not a sqlide export, or is broken."""


def workspace_to_xml(
    workspace: Workspace, *, include_passwords: bool = False
) -> str:
    """One workspace as an XML document: its name and colour, its
    connections, and the filters saved against them."""
    root = ET.Element(
        ROOT_TAG,
        {
            "format": FORMAT,
            "exported": datetime.now().isoformat(timespec="seconds"),
        },
    )
    element = ET.SubElement(root, "workspace", {"name": workspace.name})
    if workspace.color != identity.NONE:
        element.set("color", workspace.color)
    for profile in workspace.connections:
        _connection_element(element, profile, include_passwords)
    for key, entries in sorted(workspace.saved_filters.items()):
        for entry in entries:
            _filter_element(element, key, entry)
    for name, value in sorted(workspace.placeholders.items()):
        ET.SubElement(element, "placeholder", {"name": name}).text = value
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def connections_to_xml(
    profiles: list[ConnectionProfile], *, include_passwords: bool = False
) -> str:
    """Connections on their own, for importing into an existing
    workspace. Same document, with no workspace attributes to apply."""
    root = ET.Element(
        ROOT_TAG,
        {
            "format": FORMAT,
            "exported": datetime.now().isoformat(timespec="seconds"),
        },
    )
    element = ET.SubElement(root, "workspace", {"name": ""})
    for profile in profiles:
        _connection_element(element, profile, include_passwords)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def workspace_from_xml(text: str, *, name: str = "") -> Workspace:
    """Read a document back as a *new* workspace: it gets a fresh id,
    so importing the same file twice gives two independent workspaces
    rather than overwriting one. `name` overrides the file's."""
    element = _workspace_element(text)
    workspace = Workspace(
        name=name or element.get("name") or "Imported workspace",
        color=identity.normalize_color(element.get("color")),
    )
    for profile in _connections(element):
        # Through add_connection: it deduplicates the name and puts the
        # password (if the file carried one) in the keyring.
        workspace.add_connection(profile)
    for child in element:
        if child.tag == "filters":
            key = child.get("table", "")
            entry = _filter_entry(child)
            if key and entry:
                workspace.saved_filters.setdefault(key, []).append(entry)
        elif child.tag == "placeholder" and child.get("name"):
            workspace.placeholders[child.get("name")] = child.text or ""
    return workspace


def connections_from_xml(text: str) -> list[ConnectionProfile]:
    """Just the connections in a document, for merging into a workspace
    that already exists."""
    return _connections(_workspace_element(text))


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExchangeError(f"Could not read {path}: {exc}") from exc


def write(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ExchangeError(f"Could not write {path}: {exc}") from exc


# Internals


def _connection_element(
    parent: ET.Element, profile: ConnectionProfile, include_passwords: bool
) -> ET.Element:
    element = ET.SubElement(
        parent, "connection", {"name": profile.name, "kind": profile.kind}
    )
    if profile.environment != identity.UNSET:
        element.set("environment", profile.environment)
    if profile.color != identity.NONE:
        element.set("color", profile.color)
    defaults = ConnectionProfile(name="", kind="")
    for field in fields(profile):
        if field.name in ("name", "kind", "color", "environment"):
            continue  # already attributes
        value = getattr(profile, field.name)
        if field.name in SECRET_FIELDS and not include_passwords:
            continue
        if value == getattr(defaults, field.name):
            continue  # defaults are the reader's job
        child = ET.SubElement(element, field.name)
        child.text = _to_text(value)
    return element


def _filter_element(parent: ET.Element, key: str, entry: dict) -> None:
    element = ET.SubElement(
        parent,
        "filters",
        {"table": key, "name": str(entry.get("name", ""))},
    )
    for condition in entry.get("filters", []):
        ET.SubElement(
            element,
            "condition",
            {
                "column": str(condition.get("column", "")),
                "op": str(condition.get("op", "")),
                "value": str(condition.get("value", "")),
                "conjunction": str(condition.get("conjunction", "AND")),
            },
        )


def _filter_entry(element: ET.Element) -> dict | None:
    conditions = [
        {
            "column": child.get("column", ""),
            "op": child.get("op", ""),
            "value": child.get("value", ""),
            "conjunction": child.get("conjunction", "AND"),
        }
        for child in element
        if child.tag == "condition"
    ]
    if not conditions:
        return None
    return {"name": element.get("name", ""), "filters": conditions}


def _workspace_element(text: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ExchangeError(f"Not valid XML: {exc}") from exc
    if root.tag != ROOT_TAG:
        raise ExchangeError(
            f"Not a sqlide export: the document starts with <{root.tag}>, "
            f"expected <{ROOT_TAG}>"
        )
    element = root.find("workspace")
    if element is None:
        raise ExchangeError("The export contains no <workspace> element")
    return element


def _connections(element: ET.Element) -> list[ConnectionProfile]:
    types = {field.name: field.type for field in fields(ConnectionProfile)}
    profiles = []
    for child in element:
        if child.tag != "connection":
            continue
        name = child.get("name", "").strip()
        kind = child.get("kind", "").strip()
        if not name or not kind:
            raise ExchangeError(
                "A <connection> is missing its name or kind attribute"
            )
        values: dict[str, object] = {
            "name": name,
            "kind": kind,
            "color": identity.normalize_color(child.get("color")),
            "environment": identity.normalize_environment(
                child.get("environment")
            ),
        }
        for field_element in child:
            if field_element.tag not in types:
                continue  # written by a newer version: skip, don't fail
            values[field_element.tag] = _from_text(
                types[field_element.tag], field_element.text or ""
            )
        profiles.append(ConnectionProfile(**values))
    return profiles


def _to_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _from_text(annotation: object, text: str) -> object:
    """Text back to the field's type. Annotations are strings here
    (the module uses `from __future__ import annotations`), so match on
    the name rather than the type object."""
    text = text.strip()
    name = annotation if isinstance(annotation, str) else getattr(
        annotation, "__name__", ""
    )
    if name == "bool":
        return text.lower() in ("true", "1", "yes")
    if name == "int":
        try:
            return int(text)
        except ValueError:
            return 0
    return text
