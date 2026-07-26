---
title: Installation
description: Requirements and optional drivers.
order: 2
---

## Requirements

- Python 3.12+
- GTK4 + libadwaita + PyGObject — usually system packages, e.g.

  ```sh
  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
  ```

SQLite needs nothing extra beyond the above; it uses the `sqlite3` module
from the Python standard library.

## Optional drivers

Install only the extras for the databases you actually connect to:

| Target                    | Install                                |
|----------------------------|-----------------------------------------|
| MySQL                      | `pip install PyMySQL`                   |
| PostgreSQL                 | `pip install "psycopg[binary]"`         |
| JDBC (generic, experimental) | `pip install JayDeBeApi` + a Java runtime + the driver jar |
| MCP server                 | `pip install "sqlide[mcp]"`             |
| System keyring for passwords | `pip install "sqlide[keyring]"`       |

The app runs fine with SQLite alone; if you pick a connection kind whose
driver isn't installed, sqlide shows a friendly error rather than
crashing.

## Running it

```sh
python3 -m sqlide
```

Continue to [Getting Started](/docs/getting-started/) for a walkthrough
with a demo database.
