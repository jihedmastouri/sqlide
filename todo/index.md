## Index

### Foundations (do these first — most other tickets build on them)

| ID | Title | Depends on |
|---|---|---|
| CORE-01 | Object info view — every sidebar node opens something | — |
| CORE-02 | Metadata provider abstraction per engine | — |
| CORE-13 | File-system config (TOML/XML, git-friendly) | — |

### UI refinements (requested 2026-08-27 — do these first)

| ID | Title | Depends on |
|---|---|---|
| CORE-46 | Multi-language support (i18n) | CORE-13 |
| CORE-47 | Table properties in the right side panel, not a tab toggle | CORE-04, CORE-09 |
| CORE-48 | Keyword case for SQL autocompletion | CORE-13 |
| CORE-49 | Render tabular object information as a real table | CORE-01 |
| CORE-50 | Stop recycling the table properties page | CORE-47 |
| CORE-51 | Truncate the sidebar's secondary text | CORE-08 |
| CORE-52 | Sidebar click behaviour: expand, open, Open (Window) | CORE-01 |
| CORE-53 | Keep grants out of the side panel for users and roles | CORE-47, CORE-12 |
| CORE-54 | Users/roles page: scope the tree and name the subject | CORE-10, CORE-12 |
| CORE-55 | Follow the active tab in the sidebar | CORE-01, CORE-52 |
| CORE-56 | Tabular objects open in their own tab, not the properties panel | CORE-49, CORE-50, CORE-52 |
| CORE-57 | Sidebar rows are too short and clip their text | CORE-51 |
| CORE-58 | Double-click must not toggle a tree node | CORE-52 |
| CORE-59 | Unify the object info view and the properties view (done) | CORE-47, CORE-49, CORE-56 |

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

### Query builder (from RS-01)

| ID | Title | Depends on |
|---|---|---|
| CORE-17 | Query model and SQL renderer in the backend (done) | — |
| CORE-18 | Builder reads the MetadataProvider (schemas, views, capabilities) (done) | CORE-02, CORE-17 |
| CORE-19 | Persist the builder's query model in the workspace (done) | CORE-17 |
| CORE-20 | Joins: aliases, self-joins, multi-condition ON, all join kinds (done) | CORE-17, CORE-18 |
| CORE-21 | Aggregates: GROUP BY, HAVING, computed and aliased columns (done) | CORE-17 |
| CORE-22 | Builder layout: grouped filters, column search, room to grow (done) | CORE-17 |

### Table creator (from RS-02)

| ID | Title | Depends on |
|---|---|---|
| CORE-23 | Table model and DDL renderer in the backend (done) | — |
| CORE-24 | Designer reads the MetadataProvider (schemas, types, capabilities) (done) | CORE-02, CORE-23 |
| CORE-25 | Constraints and indexes in the designer (done) | CORE-23 |
| CORE-26 | Alter mode: one designer for create and alter, over a diff (done) | CORE-23, CORE-24 |
| CORE-27 | Engine-specific table and column options (done) | CORE-23, CORE-24 |
| CORE-28 | Persist the designer's table model in the workspace (done) | CORE-23 |
| CORE-29 | Copy structure from an existing table, and saved templates (done) | CORE-23, CORE-28 |

### Charting (from RS-03)

| ID | Title | Depends on |
|---|---|---|
| CORE-30 | Chart model and data mapping in the backend | CORE-02 |
| CORE-31 | Cairo chart canvas, shared with the monitoring sparkline | CORE-30 |
| CORE-32 | Chart view in the result tab, beside Data and Properties | CORE-30, CORE-31 |
| CORE-33 | Persist chart specs: with the tab, and on saved queries | CORE-30, CORE-32 |
| CORE-34 | Export a chart as PNG or SVG, and copy it | CORE-31, CORE-32 |
| CORE-35 | Dashboard tab: several saved charts, refreshed together | CORE-33 |

### Data grid and SQL (from RS-04)

| ID | Title | Depends on |
|---|---|---|
| CORE-36 | Export a result set or table to a file (done) | — |
| CORE-37 | Import a CSV file into a table (done) | CORE-36 |
| CORE-38 | Insert and delete rows in the data grid (done) | — |
| CORE-39 | Grid edits saved as one transaction | — |
| CORE-40 | Stable, efficient data-grid pagination | — |
| CORE-41 | Per-connector catalog cache | — |
| CORE-42 | Value panel and record view for wide cells (done) | — |
| CORE-43 | Foreign-key navigation in the grid (done) | CORE-41 |
| CORE-44 | Format SQL in the editor (done) | — |
| CORE-45 | Find a value across a database's tables (done) | CORE-02, CORE-43 |

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
| RS-01 | Improve the Query Builder (done — see `docs/query-builder-research.md`, filed CORE-17…CORE-22) |
| RS-02 | Improve the Table Creator (done — see `docs/table-creator-research.md`, filed CORE-23…CORE-29) |
| RS-03 | BI / charting support (done — see `docs/charting-research.md`, filed CORE-30…CORE-35) |
| RS-04 | DBeaver Comparission (done — see `docs/dbeaver-comparison.md`, filed CORE-36…CORE-45) |
