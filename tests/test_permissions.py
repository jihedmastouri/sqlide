"""The permission editor's backend: what a principal holds, and the
GRANT/REVOKE that changes it (CORE-10).

Everything the editor screen does that is not drawing lives on the
metadata provider, so it is testable without GTK and without a server:
a fake connector answers the two catalog calls the layer makes
(list_privileges for the account and for the roles it belongs to) and
records the statements it is handed. The engine-specific halves — the
privilege list per object kind, the text after ON — come from the real
PostgresMetadata and MysqlMetadata, so a dialect that drifts is caught
here rather than in front of a user.
"""

from __future__ import annotations

import pytest

from sqlide.backend.db.base import ConnectorError, PrivilegeInfo, UserInfo
from sqlide.backend.db.metadata import NodeRef
from sqlide.backend.db.mysql.metadata import MysqlMetadata
from sqlide.backend.db.postgres.metadata import PostgresMetadata
from sqlide.backend.db.sqlite.metadata import SqliteMetadata

APP = UserInfo(name="app")
MY_APP = UserInfo(name="app", host="%")


class FakeConnector:
    """Just enough connector for the permission layer: privileges by
    account name, plus a record of what was executed."""

    def __init__(
        self,
        privileges=None,
        quote='"',
        fail_on=None,
        object_grants=None,
        users=None,
    ) -> None:
        self.privileges = privileges or {}
        self.object_grants = object_grants or {}
        self.users = users or [
            UserInfo(name=name) for name in sorted(self.privileges)
        ]
        self._quote = quote
        self.executed: list[str] = []
        self.fail_on = fail_on
        self.database = "sales"

    def quote_ident(self, name: str) -> str:
        if self._quote == '"':
            return '"' + name.replace('"', '""') + '"'
        return "`" + name.replace("`", "``") + "`"

    def account_ident(self, user: UserInfo) -> str:
        if user.host:
            return f"'{user.name}'@'{user.host}'"
        return self.quote_ident(user.name)

    def list_privileges(self, user: UserInfo) -> list[PrivilegeInfo]:
        return list(self.privileges.get(user.name, []))

    def __getattr__(self, name: str):
        # The descriptor builders (db/objects.py) call the whole
        # Connector listing surface. Anything this fake does not
        # implement answers with an empty list, which is the same
        # contract an adapter without that catalog keeps.
        if name.startswith(("list_", "table_", "get_")):
            return lambda *args, **kwargs: []
        raise AttributeError(name)

    def list_users(self) -> list[UserInfo]:
        return list(self.users)

    def list_object_grants(self, kind: str, name: str):
        return list(self.object_grants.get((kind, name), []))

    def current_schema(self) -> str:
        return "public"

    def execute(self, sql: str):
        self.executed.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise ConnectorError("permission denied")
        return 0


def pg(privileges=None, **kwargs) -> PostgresMetadata:
    return PostgresMetadata(FakeConnector(privileges, **kwargs))


def my(privileges=None, **kwargs) -> MysqlMetadata:
    return MysqlMetadata(FakeConnector(privileges, quote="`", **kwargs))


# The engine-correct privilege list


def test_postgres_offers_the_privileges_of_each_kind() -> None:
    provider = pg()
    table = provider.privileges_for(
        NodeRef("table", "orders", schema="public")
    )
    assert table == (
        "SELECT", "INSERT", "UPDATE", "DELETE",
        "TRUNCATE", "REFERENCES", "TRIGGER",
    )
    assert provider.privileges_for(NodeRef("schema", "public")) == (
        "USAGE", "CREATE",
    )
    assert provider.privileges_for(
        NodeRef("function", "total", schema="public")
    ) == ("EXECUTE",)
    assert provider.privileges_for(NodeRef("database", "sales")) == (
        "CONNECT", "CREATE", "TEMPORARY",
    )
    # A node GRANT cannot name offers nothing rather than guessing.
    assert provider.privileges_for(NodeRef("category", "Tables")) == ()
    assert provider.privileges_for(NodeRef("connection", "local")) == ()


def test_mysql_narrows_the_grant_list_as_the_target_narrows() -> None:
    provider = my()
    server = provider.privileges_for(NodeRef("connection", "local"))
    database = provider.privileges_for(NodeRef("database", "sales"))
    table = provider.privileges_for(
        NodeRef("table", "orders", database="sales")
    )
    assert "RELOAD" in server and "RELOAD" not in database
    assert "LOCK TABLES" in database and "LOCK TABLES" not in table
    assert set(table) < set(database) < set(server)
    assert provider.privileges_for(
        NodeRef("column", "total", database="sales", table="orders")
    ) == ("SELECT", "INSERT", "UPDATE", "REFERENCES")


def test_sqlite_has_no_permission_editor() -> None:
    assert not SqliteMetadata.CAPABILITIES.permission_editor
    assert not SqliteMetadata.CAPABILITIES.grants


def test_grant_targets_are_the_dialects_own() -> None:
    assert pg().grant_target(
        NodeRef("table", "orders", schema="public")
    ) == 'TABLE "public"."orders"'
    assert pg().grant_target(NodeRef("schema", "sales")) == 'SCHEMA "sales"'
    assert my().grant_target(NodeRef("connection", "local")) == "*.*"
    assert my().grant_target(NodeRef("database", "sales")) == "`sales`.*"
    assert my().grant_target(
        NodeRef("column", "total", database="sales", table="orders")
    ) == "`sales`.`orders`"


# What a principal holds


def test_direct_grants_are_reported_and_editable() -> None:
    provider = pg({
        "app": [
            PrivilegeInfo("table public.orders", "SELECT"),
            PrivilegeInfo("table public.orders", "UPDATE", grantable=True),
            PrivilegeInfo("table public.other", "DELETE"),
        ]
    })
    permissions = provider.permission_set(
        APP, NodeRef("table", "orders", schema="public")
    )
    assert permissions.target == 'TABLE "public"."orders"'
    assert permissions.state("SELECT").granted
    assert not permissions.state("SELECT").grantable
    assert permissions.state("UPDATE").grantable
    # A grant on another object does not leak onto this one.
    assert not permissions.state("DELETE").granted
    assert all(entry.editable for entry in permissions.entries)


def test_inherited_privileges_name_their_role_and_are_not_editable() -> None:
    provider = pg({
        "app": [
            PrivilegeInfo("role membership", "member of analysts"),
            PrivilegeInfo("table public.orders", "SELECT"),
        ],
        "analysts": [
            PrivilegeInfo("table public.orders", "INSERT", grantable=True),
        ],
    })
    permissions = provider.permission_set(
        APP, NodeRef("table", "orders", schema="public")
    )
    direct = permissions.state("SELECT")
    inherited = permissions.state("INSERT")
    assert direct.granted and direct.editable and not direct.inherited_from
    assert inherited.granted and inherited.inherited_from == "analysts"
    assert not inherited.editable


def test_a_direct_grant_wins_over_the_same_one_inherited() -> None:
    provider = pg({
        "app": [
            PrivilegeInfo("role membership", "member of analysts"),
            PrivilegeInfo("table public.orders", "SELECT"),
        ],
        "analysts": [PrivilegeInfo("table public.orders", "SELECT")],
    })
    state = provider.permission_set(
        APP, NodeRef("table", "orders", schema="public")
    ).state("SELECT")
    assert state.granted and state.editable


# The statements


def _table(provider, privileges=None):
    return provider.permission_set(
        APP if isinstance(provider, PostgresMetadata) else MY_APP,
        NodeRef(
            "table", "orders",
            schema="public" if isinstance(provider, PostgresMetadata) else "",
            database="sales",
        ),
    )


def test_ticking_a_box_builds_one_grant() -> None:
    provider = pg()
    current = _table(provider)
    assert provider.permission_statements(
        APP, current, {"SELECT": (True, False), "INSERT": (True, False)}
    ) == ['GRANT SELECT, INSERT ON TABLE "public"."orders" TO "app"']


def test_grant_option_is_its_own_statement_both_ways() -> None:
    provider = pg({
        "app": [PrivilegeInfo("table public.orders", "UPDATE", grantable=True)]
    })
    current = _table(provider)
    assert provider.permission_statements(
        APP, current, {"SELECT": (True, True)}
    ) == [
        'GRANT SELECT ON TABLE "public"."orders" TO "app" '
        "WITH GRANT OPTION"
    ]
    # Taking the option away leaves the privilege itself in place.
    assert provider.permission_statements(
        APP, current, {"UPDATE": (True, False)}
    ) == [
        'REVOKE GRANT OPTION FOR UPDATE ON TABLE "public"."orders" '
        'FROM "app"'
    ]


def test_unticking_builds_a_revoke() -> None:
    provider = pg({"app": [PrivilegeInfo("table public.orders", "DELETE")]})
    current = _table(provider)
    assert provider.permission_statements(
        APP, current, {"DELETE": (False, False)}
    ) == ['REVOKE DELETE ON TABLE "public"."orders" FROM "app"']


def test_unchanged_and_inherited_privileges_produce_nothing() -> None:
    provider = pg({
        "app": [
            PrivilegeInfo("role membership", "member of analysts"),
            PrivilegeInfo("table public.orders", "SELECT"),
        ],
        "analysts": [PrivilegeInfo("table public.orders", "INSERT")],
    })
    current = _table(provider)
    assert provider.permission_statements(
        APP,
        current,
        # SELECT as it already is, INSERT only held through the role.
        {"SELECT": (True, False), "INSERT": (False, False)},
    ) == []


def test_a_mysql_column_grant_names_its_column() -> None:
    provider = my()
    current = provider.permission_set(
        MY_APP,
        NodeRef("column", "total", database="sales", table="orders"),
    )
    assert provider.permission_statements(
        MY_APP, current, {"SELECT": (True, False)}
    ) == ["GRANT SELECT (`total`) ON `sales`.`orders` TO 'app'@'%'"]


def test_a_privilege_the_object_cannot_carry_is_refused() -> None:
    from sqlide.backend.db.metadata import PermissionSet, PrivilegeState

    provider = pg()
    current = PermissionSet(
        ref=NodeRef("schema", "public"),
        target='SCHEMA "public"',
        entries=(PrivilegeState("TRUNCATE"),),
    )
    with pytest.raises(ConnectorError):
        provider.permission_statements(APP, current, {"TRUNCATE": (True, False)})


# Running them


def test_postgres_runs_the_batch_in_one_transaction() -> None:
    provider = pg()
    provider.apply_permissions(["GRANT A", "GRANT B"])
    assert provider.connector.executed == [
        "BEGIN", "GRANT A", "GRANT B", "COMMIT",
    ]


def test_a_failure_rolls_back_and_names_the_statement() -> None:
    provider = pg(fail_on="GRANT B")
    with pytest.raises(ConnectorError) as failure:
        provider.apply_permissions(["GRANT A", "GRANT B", "GRANT C"])
    assert "GRANT B" in str(failure.value)
    assert provider.connector.executed == [
        "BEGIN", "GRANT A", "GRANT B", "ROLLBACK",
    ]


def test_mysql_has_no_transaction_to_wrap_grants_in() -> None:
    provider = my()
    assert not provider.capabilities().transactional_grants
    provider.apply_permissions(["GRANT A"])
    assert provider.connector.executed == ["GRANT A"]


def test_mysql_stops_at_the_statement_that_failed() -> None:
    provider = my(fail_on="GRANT B")
    with pytest.raises(ConnectorError) as failure:
        provider.apply_permissions(["GRANT A", "GRANT B", "GRANT C"])
    assert "GRANT B" in str(failure.value)
    assert provider.connector.executed == ["GRANT A", "GRANT B"]


def test_nothing_pending_runs_nothing() -> None:
    provider = pg()
    provider.apply_permissions([])
    assert provider.connector.executed == []


# Who holds what on one object (CORE-11)


ORDERS = NodeRef("table", "orders", schema="public")


def test_object_grants_name_the_principal_and_the_grantor() -> None:
    provider = pg(object_grants={("table", "orders"): [
        PrivilegeInfo("role app", "SELECT", grantor="postgres"),
        PrivilegeInfo("role app", "UPDATE", grantable=True, grantor="owner"),
    ]})
    grants = provider.object_grants(ORDERS)
    assert [(g.principal, g.privilege, g.source, g.grantor, g.grantable)
            for g in grants] == [
        ("app", "SELECT", "direct", "postgres", False),
        ("app", "UPDATE", "direct", "owner", True),
    ]


def test_a_role_grant_is_reported_against_its_members_too() -> None:
    """The inverse of the editor's rule: a member holds what the role
    holds, and the row says which role it arrives through."""
    provider = pg(
        privileges={
            "app": [PrivilegeInfo("role membership", "member of analysts")],
            "analysts": [],
        },
        object_grants={("table", "orders"): [
            PrivilegeInfo("role analysts", "SELECT"),
        ]},
    )
    grants = provider.object_grants(ORDERS)
    assert [(g.principal, g.via, g.source) for g in grants] == [
        ("analysts", "", "direct"),
        ("app", "analysts", "via analysts"),
    ]


def test_public_grants_are_one_explicit_row() -> None:
    provider = pg(
        privileges={"app": [], "analysts": []},
        object_grants={("table", "orders"): [
            PrivilegeInfo("role PUBLIC", "SELECT"),
        ]},
    )
    grants = provider.object_grants(ORDERS)
    assert len(grants) == 1
    assert grants[0].public and grants[0].source == "everyone"


def test_only_the_kinds_that_carry_an_acl_have_a_permissions_section() -> None:
    provider = pg(object_grants={("index", "orders_pkey"): [
        PrivilegeInfo("role app", "SELECT"),
    ]})
    assert provider.object_grants(NodeRef("index", "orders_pkey")) == []


def test_sqlite_reports_no_object_grants() -> None:
    provider = SqliteMetadata(FakeConnector())
    assert provider.object_grants(NodeRef("table", "notes")) == []
    assert "permissions" not in provider.property_sections()


def test_the_permissions_section_links_into_the_editor() -> None:
    """Each row opens that principal in the CORE-10 editor, scoped to
    the object the section belongs to; a PUBLIC row opens nothing."""
    provider = pg(object_grants={("table", "orders"): [
        PrivilegeInfo("role app", "SELECT"),
        PrivilegeInfo("role PUBLIC", "SELECT"),
    ]})
    info = provider.table_properties(ORDERS)
    section = next(t for t in info.tables if t.slug == "permissions")
    assert section.columns[:3] == ["Principal", "Privilege", "Source"]
    assert [row[0] for row in section.rows] == ["app", "PUBLIC"]
    link = section.link(0)
    assert link.kind == "principal" and link.name == "app"
    assert link.table == "orders" and link.category == "table"
    assert section.link(1) is None


def test_mysql_reports_no_grantor_but_still_names_the_grantee() -> None:
    """MySQL's catalog has no GRANTOR column, so the column is empty
    rather than invented."""
    provider = my(object_grants={("table", "orders"): [
        PrivilegeInfo("user 'app'@'%'", "SELECT"),
    ]})
    grants = provider.object_grants(NodeRef("table", "orders", database="sales"))
    assert [(g.principal, g.grantor) for g in grants] == [("'app'@'%'", "")]
