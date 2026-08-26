---
title: Monitoring
description: Sessions, throughput and storage over an ordinary connection.
order: 8
---

Right-click a connection in the sidebar and pick **Monitoring…** to open
its dashboard: what the server is doing right now, read over the same
client connection everything else uses. PostgreSQL and MySQL (and
MariaDB) have one; SQLite is a file with no server to ask, so the item
is not offered.

The dashboard opens a connection of its own, so polling never
interleaves with your transactions, and closes it — with both its
timers — when you close the tab.

## What it shows

- **Sessions** — every backend or thread the server will name, with its
  user, database, state, duration and SQL. Sortable by any column and
  filterable. Selecting a row offers **Cancel Query** (the statement
  stops, the connection stays) and **End Session** (the connection
  closes and anything uncommitted rolls back); both show what they are
  about to stop and ask first, and neither is offered on the
  dashboard's own connection.
- **Throughput and health** — transactions or queries per second,
  sessions against `max_connections`, cache hit ratio and lock waits,
  each a sparkline over the last five minutes. Nothing is persisted:
  closing the tab forgets it.
- **Storage** — database sizes and the largest tables. Sizes are the
  expensive question and the slowest-changing answer, so they have
  their own 60-second timer.
- **Not available** — one row for every panel this connection cannot
  fill, naming the grant, extension or server version that would fill
  it. A panel is never a blank chart.

Polling defaults to every 2 seconds — about a millisecond of server
work, safe on production — and is adjustable from 1 to 60 seconds in the
header, or pausable outright. The interval you settle on is remembered
in `settings.toml` as `monitor_interval` and is what the next dashboard
opens with. **Refresh** takes a single sample whether or not polling is
paused.

## Counters, not levels

Every throughput number both engines publish (`xact_commit`,
`Questions`, `blks_hit`, …) counts since the server started or since the
statistics were last reset, so the dashboard charts the change between
samples. A cache hit ratio here is therefore the ratio *for the minute
you are watching*, not the lifetime average a month-old server would
otherwise flatten it into. When a counter goes backwards — the server
restarted, or someone ran `pg_stat_reset()` — the lines start again from
that point and a banner says why, rather than dipping through a spike
that never happened.

## When it can see less than the whole server

This is the part worth knowing before you trust a quiet dashboard.
Neither engine refuses an unprivileged account outright; both quietly
show it less:

- **PostgreSQL** returns a row per backend but blanks other sessions'
  state and SQL as `<insufficient privilege>`. The fix is one grant:
  `GRANT pg_monitor TO <role>`.
- **MySQL** lists only your own account's threads from `SHOW
  PROCESSLIST` and reports no error at all. The fix is `GRANT PROCESS,
  REPLICATION CLIENT ON *.*`, plus `SELECT` on `performance_schema` and
  `sys` for the instrumentation panels.

Both look exactly like an idle server. The dashboard compares what it
was shown against what the server says exists and puts a banner above
the session list when they disagree, so a thin dashboard always says
whether the server is quiet or the account is.

## What it will never show

The host's CPU, memory, disk space and disk I/O. Neither engine exposes
them over SQL — PostgreSQL knows how many buffers it read, not how much
RAM is left — and a number derived from a proxy would be a guess with a
gauge drawn around it. Getting them needs an agent, SSH or a metrics
endpoint, none of which a database client may assume. The footer of the
dashboard says so, once.

The reasoning and the measurements behind all of this are in the
[monitoring spike](/docs/monitoring-spike/).
