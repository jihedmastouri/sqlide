"""Connection profiles.

A profile describes how to reach one database, and how much a mistake
against it costs (identity colour + environment class, see
backend/identity.py). Profiles belong to a workspace and persist
inside its file (see backend/workspaces.py).
password/ssh_password are written to the system keyring when one is
available (backend/secrets.py) and blanked out of the JSON; otherwise
they fall back to plain text in the file, as in earlier versions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlide.backend import identity


@dataclass
class ConnectionProfile:
    name: str
    kind: str  # "sqlite" | "mysql" | "postgres" | "jdbc"
    # Identity (backend/identity.py): the colour this connection wears
    # in the sidebar, its tabs and the status bar, and the environment
    # class that decides how much friction destructive actions carry.
    color: str = identity.NONE
    environment: str = identity.UNSET
    file_path: str = ""  # sqlite only
    host: str = "localhost"
    port: int = 0  # 0 -> adapter default (3306 / 5432)
    user: str = ""
    password: str = ""
    database: str = ""
    # postgres only: the schema to work in, pinned as the connection's
    # search_path. Empty keeps the server's own search_path (usually
    # "$user", public). MySQL needs no equivalent — there a schema and
    # a database are one object, so `database` above is already it.
    schema: str = ""
    jdbc_url: str = ""  # jdbc only, e.g. jdbc:h2:/path/to/db
    driver_class: str = ""  # jdbc only, e.g. org.h2.Driver
    jar_path: str = ""  # jdbc only, path to the driver jar
    # Advanced, server kinds (mysql/postgres) only. SSL: mode "" keeps
    # the driver default; the paths are optional PEM files.
    ssl_mode: str = ""  # "" | "disable" | "require" | "verify-ca" | "verify-full"
    ssl_ca: str = ""
    ssl_cert: str = ""
    ssl_key: str = ""
    # SSH tunnel: when enabled, the adapter connects through a local
    # forward to ssh_host instead of reaching host:port directly.
    use_ssh: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key_path: str = ""

    def __post_init__(self) -> None:
        # A file written by a newer version (or edited by hand) must
        # still open: an unknown colour or environment degrades to the
        # neutral default instead of failing to load.
        self.color = identity.normalize_color(self.color)
        self.environment = identity.normalize_environment(self.environment)

    def ssl_params(self) -> dict | None:
        """SSL settings for the adapter, or None when untouched."""
        if not (self.ssl_mode or self.ssl_ca or self.ssl_cert or self.ssl_key):
            return None
        return {
            "mode": self.ssl_mode,
            "ca": self.ssl_ca,
            "cert": self.ssl_cert,
            "key": self.ssl_key,
        }

    def ssh_params(self) -> dict | None:
        """SSH tunnel settings for the adapter, or None when disabled."""
        if not self.use_ssh:
            return None
        return {
            "host": self.ssh_host,
            "port": self.ssh_port or 22,
            "user": self.ssh_user,
            "password": self.ssh_password,
            "key_path": self.ssh_key_path,
        }

    def connect_params(self) -> dict:
        """Keyword args for registry.create_connector()."""
        if self.kind == "sqlite":
            return {"file_path": self.file_path}
        if self.kind == "jdbc":
            return {
                "url": self.jdbc_url,
                "driver_class": self.driver_class,
                "jar_path": self.jar_path,
                "user": self.user,
                "password": self.password,
            }
        params = {
            "host": self.host,
            "port": self.port or {"mysql": 3306, "postgres": 5432}[self.kind],
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "ssl": self.ssl_params(),
            "ssh": self.ssh_params(),
        }
        if self.kind == "postgres":
            params["schema"] = self.schema
        return params

    def to_dict(self) -> dict:
        return asdict(self)
