"""Connection profiles.

A profile describes how to reach one database. Profiles belong to a
workspace and persist inside its file (see backend/workspaces.py).
password/ssh_password are written to the system keyring when one is
available (backend/secrets.py) and blanked out of the JSON; otherwise
they fall back to plain text in the file, as in earlier versions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class ConnectionProfile:
    name: str
    kind: str  # "sqlite" | "mysql" | "postgres" | "jdbc"
    file_path: str = ""  # sqlite only
    host: str = "localhost"
    port: int = 0  # 0 -> adapter default (3306 / 5432)
    user: str = ""
    password: str = ""
    database: str = ""
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
        return {
            "host": self.host,
            "port": self.port or {"mysql": 3306, "postgres": 5432}[self.kind],
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "ssl": self.ssl_params(),
            "ssh": self.ssh_params(),
        }

    def to_dict(self) -> dict:
        return asdict(self)
