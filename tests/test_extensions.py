"""Extension-aware handling (PG-05).

Three layers, tested in the order they stack:

* the registry (`backend/db/extensions.py`) — what is known about an
  extension, what a version comparison means, and the SQL each action
  produces. No connection involved;
* the metadata provider — the one listing every question is asked of,
  the features it unlocks, the privilege gate on the actions, and the
  PostGIS check PG-04 already relied on, now answered through the
  registry rather than by name;
* the live server, where `pg_available_extensions` says what is
  installed and what merely could be, and an extension-owned object is
  attributed to its extension instead of reading as a stray.
"""

from __future__ import annotations

import pytest

from sqlide.backend.db import extensions as ext, objects, registry
from sqlide.backend.db.base import ObjectSummary
from sqlide.backend.db.metadata import MetadataProvider, NodeRef
from sqlide.backend.db.postgres.metadata import PostgresMetadata

# The registry


def test_a_known_extension_carries_its_features() -> None:
    postgis = ext.trait("postgis")
    assert postgis.known and postgis.title == "PostGIS"
    assert postgis.has("spatial")
    assert "geometry" in postgis.types


def test_the_ticket_s_extensions_are_all_registered() -> None:
    for name in (
        "postgis", "pg_stat_statements", "timescaledb", "vector",
        "pg_cron", "uuid-ossp", "hstore", "citext",
    ):
        assert ext.trait(name).known, name


def test_an_unknown_extension_gets_the_generic_trait() -> None:
    """No errors, no special-casing: an extension nobody registered is
    named after itself, unlocks nothing, and still lists."""
    unknown = ext.trait("wildly_bespoke")
    assert not unknown.known
    assert unknown.title == "wildly_bespoke"
    assert unknown.features == () and unknown.types == ()


def test_an_update_is_available_when_the_default_differs() -> None:
    current = ext.ExtensionState("postgis", version="3.4.2", schema="public")
    assert current.installed and not current.update_available
    behind = ext.ExtensionState(
        "postgis", version="3.4.2", schema="public", default_version="3.5.0"
    )
    assert behind.update_available
    assert "update to 3.5.0" in behind.detail()
    assert "3.4.2 in public" in behind.detail()


def test_an_available_extension_is_not_installed() -> None:
    available = ext.ExtensionState("hstore", default_version="1.8")
    assert not available.installed and not available.update_available
    assert available.detail() == "1.8 available"


def test_features_and_type_owners_come_from_what_is_installed() -> None:
    states = [
        ext.ExtensionState("postgis", version="3.4.2"),
        ext.ExtensionState("vector", default_version="0.7.0"),  # available
    ]
    assert ext.features(states) == {"spatial", "types"}
    assert ext.type_owner("geometry", states) == "postgis"
    # Only installed extensions own anything: pgvector is on disk here,
    # not in the database.
    assert ext.type_owner("vector", states) == ""


def test_the_statements_are_plain_reviewable_sql() -> None:
    assert ext.install_sql("hstore") == 'CREATE EXTENSION "hstore"'
    assert (
        ext.install_sql("hstore", schema="util")
        == 'CREATE EXTENSION "hstore" SCHEMA "util"'
    )
    # A hyphenated name has to be quoted or it will not parse.
    assert ext.install_sql("uuid-ossp") == 'CREATE EXTENSION "uuid-ossp"'
    assert ext.update_sql("postgis") == 'ALTER EXTENSION "postgis" UPDATE'
    assert (
        ext.update_sql("postgis", version="3.5.0")
        == "ALTER EXTENSION \"postgis\" UPDATE TO '3.5.0'"
    )
    assert ext.drop_sql("postgis") == 'DROP EXTENSION "postgis"'
    assert (
        ext.drop_sql("postgis", cascade=True)
        == 'DROP EXTENSION "postgis" CASCADE'
    )


# The provider


class _Connector:
    """Just enough connector for the extension questions."""

    def __init__(self, states=(), *, superuser=False, owner="") -> None:
        self._states = list(states)
        self._superuser = superuser
        self._owner = owner
        self.asked: list[tuple[str, str]] = []

    def list_extensions(self):
        return list(self._states)

    def can_manage_extensions(self) -> bool:
        return self._superuser

    def extension_owner(self, name: str, schema: str = "") -> str:
        self.asked.append((name, schema))
        return self._owner

    def quote_ident(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def list_catalog(self, slug: str, schema: str = ""):
        return []

    def __getattr__(self, name):
        # Every other listing a descriptor might reach for: empty, the
        # way an adapter that has no such catalog answers.
        return lambda *args, **kwargs: []


def _provider(**kwargs) -> PostgresMetadata:
    return PostgresMetadata(_Connector(**kwargs))


def test_an_engine_without_extensions_lists_none() -> None:
    generic = MetadataProvider(_Connector([ext.ExtensionState("postgis")]))
    assert not generic.capabilities().extensions
    assert generic.extensions() == []
    assert generic.extension_features() == set()
    assert not generic.can_manage_extensions()
    assert generic.spatial_extension() == ""


def test_the_provider_splits_installed_from_available() -> None:
    provider = _provider(states=[
        ext.ExtensionState("postgis", version="3.4.2", schema="public"),
        ext.ExtensionState("hstore", default_version="1.8"),
    ])
    assert [s.name for s in provider.extensions()] == ["postgis", "hstore"]
    assert [s.name for s in provider.installed_extensions()] == ["postgis"]


def test_features_gate_on_what_is_installed() -> None:
    provider = _provider(states=[
        ext.ExtensionState("timescaledb", version="2.14.2"),
    ])
    assert provider.has_extension_feature("hypertables")
    assert not provider.has_extension_feature("spatial")


def test_the_map_gate_is_now_the_registry_s_spatial_feature() -> None:
    """PG-04's check, generalised: the answer comes from the feature,
    not from an extension named in the provider."""
    assert _provider(states=[
        ext.ExtensionState("postgis", version="3.4.2", schema="public"),
    ]).spatial_extension() == "postgis 3.4.2"
    assert _provider(states=[
        ext.ExtensionState("hstore", version="1.8"),
    ]).spatial_extension() == ""


def test_an_adapter_with_only_the_catalog_folder_still_answers() -> None:
    """The fallback path: an adapter that has the Extensions folder but
    no structured listing is parsed back into states, so PG-04's gate
    keeps working wherever it worked before."""

    class OnlyCatalog:
        def list_catalog(self, slug, schema=""):
            assert slug == "extensions"
            return [
                ObjectSummary(
                    name="postgis", kind="extension", detail="3.4.2 in public"
                )
            ]

    provider = PostgresMetadata(OnlyCatalog())
    state = provider.extensions()[0]
    assert (state.name, state.version, state.schema) == (
        "postgis", "3.4.2", "public"
    )
    assert provider.spatial_extension() == "postgis 3.4.2"


def test_the_actions_are_gated_on_the_account() -> None:
    assert not _provider().can_manage_extensions()
    assert _provider(superuser=True).can_manage_extensions()


def test_the_actions_build_reviewable_statements() -> None:
    provider = _provider()
    assert provider.extension_statements("install", "uuid-ossp") == [
        'CREATE EXTENSION "uuid-ossp"'
    ]
    assert provider.extension_statements("update", "postgis") == [
        'ALTER EXTENSION "postgis" UPDATE'
    ]
    assert provider.extension_statements("drop", "postgis", cascade=True) == [
        'DROP EXTENSION "postgis" CASCADE'
    ]
    # A verb nobody offers produces nothing rather than guessing.
    assert provider.extension_statements("frobnicate", "postgis") == []


def test_a_broken_catalog_costs_the_listing_and_nothing_else() -> None:
    class Broken:
        def list_extensions(self):
            raise RuntimeError("permission denied")

        def list_catalog(self, slug, schema=""):
            raise RuntimeError("permission denied")

        def can_manage_extensions(self):
            raise RuntimeError("permission denied")

    provider = PostgresMetadata(Broken())
    assert provider.extensions() == []
    assert provider.spatial_extension() == ""
    assert not provider.can_manage_extensions()


# Attribution in the info views


def test_an_extension_owned_object_says_whose_it_is() -> None:
    connector = _Connector(owner="postgis")
    info = objects.describe(connector, "table", "spatial_ref_sys")
    assert ("Extension", "postgis (PostGIS)") in info.summary


def test_a_user_object_is_attributed_to_nobody() -> None:
    connector = _Connector(owner="")
    info = objects.describe(connector, "table", "orders")
    assert not [row for row in info.summary if row[0] == "Extension"]


def test_a_column_is_attributed_through_its_table_not_itself() -> None:
    connector = _Connector(owner="postgis")
    objects.describe(connector, "column", "geom", table="places")
    assert connector.asked == []


def test_the_extension_info_view_explains_a_known_extension() -> None:
    info = objects.describe(
        _Connector(), "extension", "postgis", detail="3.4.2 in public"
    )
    summary = dict(info.summary)
    assert summary["Known as"] == "PostGIS"
    assert "Enables" in summary


def test_the_extension_info_view_of_an_unknown_one_still_opens() -> None:
    info = objects.describe(
        _Connector(), "extension", "wildly_bespoke", detail="0.1"
    )
    assert info.name == "wildly_bespoke"
    assert dict(info.summary)["Detail"] == "0.1"
    assert "Known as" not in dict(info.summary)


# Against a live server


@pytest.fixture()
def pg_extensions(postgres):
    _version, connector = postgres
    return registry.create_provider("postgres", connector)


def test_the_server_reports_installed_and_available(pg_extensions) -> None:
    states = pg_extensions.extensions()
    assert states, "pg_available_extensions is never empty"
    installed = {s.name for s in states if s.installed}
    available = {s.name for s in states if not s.installed}
    # plpgsql is installed in every stock database, and the contrib
    # extensions on the image are available and not installed.
    assert "plpgsql" in installed
    assert available and not (installed & available)
    plpgsql = next(s for s in states if s.name == "plpgsql")
    assert plpgsql.version and plpgsql.schema


def test_the_two_folders_hold_the_two_halves(pg_extensions) -> None:
    database = NodeRef("database", "sqlide")
    folders = {
        folder.name: folder
        for folder in pg_extensions.list_children(database)
        if folder.kind == "category"
    }
    installed = pg_extensions.list_children(folders["Extensions"])
    available = pg_extensions.list_children(folders["Available Extensions"])
    assert "plpgsql" in [row.name for row in installed]
    assert "plpgsql" not in [row.name for row in available]
    assert {row.kind for row in installed} == {"extension"}
    # Every row opens the generic info view, known extension or not.
    assert pg_extensions.describe(installed[0]).summary


def test_a_server_object_is_attributed_to_its_extension(
    pg_extensions,
) -> None:
    """plpgsql's validator function belongs to plpgsql, and the info
    view says so instead of showing a stray function."""
    connector = pg_extensions.connector
    assert connector.extension_owner("plpgsql_validator", "pg_catalog") == (
        "plpgsql"
    )
    assert connector.extension_owner("no_such_object_anywhere") == ""


def test_managing_extensions_is_answerable_on_a_live_server(
    pg_extensions,
) -> None:
    # The test server connects as a superuser, but the point is that
    # the question is answered rather than raised.
    assert pg_extensions.can_manage_extensions() in (True, False)
