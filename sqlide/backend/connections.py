"""Connection profiles.

A profile describes how to reach one database. Profiles belong to a
workspace and persist inside its file (see backend/workspaces.py).
Known v1 limitation: passwords are stored in plain text.
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
        }

    def to_dict(self) -> dict:
        return asdict(self)
