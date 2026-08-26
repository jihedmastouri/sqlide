## Index

### Foundations (do these first — most other tickets build on them)

| ID | Title | Depends on |
|---|---|---|
| CORE-01 | Object info view — every sidebar node opens something | — |
| CORE-02 | Metadata provider abstraction per engine | — |
| CORE-13 | File-system config (TOML/XML, git-friendly) | — |

### Cross-engine UI

| ID | Title | Depends on |
|---|---|---|
| CORE-03 | Sidebar search mode (Exit + Filter, hide other actions) | — |
| CORE-04 | Table tab: Data / Properties toggle | CORE-01 |
| CORE-05 | Deep-link from sidebar into a specific properties section | CORE-04 |
| CORE-06 | Connection context menu: Disconnect | — |
| CORE-07 | Connection context menu: Close all related tabs | — |
| CORE-08 | Resizable left sidebar with scrolling | — |
| CORE-09 | Notes panel in the right sidebar | CORE-13 |
| CORE-16 | Graphical Explain | - |

### Users, roles, permissions

| ID | Title | Depends on |
|---|---|---|
| CORE-10 | Permission editor — split screen tree + grid | CORE-02 |
| CORE-11 | Grantees section on object properties | CORE-04, CORE-10 |
| CORE-12 | Users/roles list overview | CORE-02 |

### Monitoring

| ID | Title | Depends on |
|---|---|---|
| CORE-14 | Spike: resource & usage monitoring feasibility (PG + MySQL) | — |
| CORE-15 | Resource & usage monitoring dashboard | CORE-14 |

### Postgres

| ID | Title | Depends on |
|---|---|---|
| PG-01 | Schema as a first-class level in the object model | CORE-02 |
| PG-02 | Postgres sidebar tree — full node set | PG-01, CORE-01 |
| PG-03 | Dim `information_schema` and system schemas | PG-02 |
| PG-04 | Geo/PostGIS data viewer on OpenStreetMap | PG-01 |
| PG-05 | Extension-aware handling and per-extension UI | PG-01 |

### MySQL

| ID | Title | Depends on |
|---|---|---|
| MY-01 | MySQL sidebar tree — full node set | CORE-01, CORE-02 |

### SQLite

| ID | Title | Depends on |
|---|---|---|
| SQ-01 | SQLite sidebar tree — full node set | CORE-01, CORE-02 |
| SQ-02 | PRAGMA viewer and editor | — |

### Research / not yet specced

| ID | Title |
|---|---|
| RS-01 | Improve the Query Builder |
| RS-02 | Improve the Table Creator |
| RS-03 | BI / charting support |
| RS-04 | DBeaver Comparission |
