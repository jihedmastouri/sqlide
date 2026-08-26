---
title: Monitoring Spike
description: What Postgres and MySQL will tell a plain client about themselves.
order: 12
---

This is the write-up of CORE-14: can sqlide show useful resource and
usage monitoring over an ordinary client connection, and what does it
ask of the user? The short answer is **yes, go** — with one hard limit
and one soft one.

The hard limit is the host. CPU, memory, disk space and disk I/O of the
*machine* are not exposed over SQL by either engine. PostgreSQL knows
how many buffers it read, not how much RAM is left; MySQL knows its
buffer pool hit rate, not the host's load average. Nothing short of an
agent, SSH or a metrics endpoint gets those, and all three are outside
what a database client may assume. **The dashboard must say so once, in
words, and never draw a CPU gauge it filled from a proxy.**

The soft limit is privilege. Every server-wide view degrades for an
unprivileged account, and — this is the finding that shapes the UI —
usually by *hiding rows rather than raising an error*. A read-only
report user watching the dashboard sees a server with one session and
no queries, which is indistinguishable from an idle server. So each
panel has to know why it is thin, which is what
`sqlide/backend/db/monitoring.py` exists to answer.

Everything below was measured against the test matrix in
`docker-compose.yml` (PostgreSQL 10–16, MySQL 5.7 and 8.0), and the
claims are asserted in `tests/test_monitoring.py`.

## PostgreSQL

| Metric | Source | Needs | Notes |
| --- | --- | --- | --- |
| Sessions: user, db, state, wait, duration, SQL | `pg_stat_activity` | CONNECT for your own rows; `pg_read_all_stats` / `pg_monitor` / superuser for everyone's | degrades by masking, see below |
| Commits, rollbacks, tuples, temp files, deadlocks | `pg_stat_database` | CONNECT | world-readable |
| Cache hit ratio | `blks_hit` / `blks_read` in `pg_stat_database` | CONNECT | ratio since stats reset — chart the delta, not the total |
| Checkpoints, buffers written | `pg_stat_bgwriter` | CONNECT | split into `pg_stat_checkpointer` in PG 17 |
| I/O by backend type and context | `pg_stat_io` | **PostgreSQL 16+** | does not exist below 16; timings only with `track_io_timing = on` |
| Locks, and who blocks whom | `pg_locks` joined to `pg_stat_activity` | CONNECT; the blocking session's SQL needs the stats role | `pg_blocking_pids()` is the cheap join |
| Database and table sizes | `pg_database_size()`, `pg_total_relation_size()` | CONNECT on the database, and object visibility | sizes of databases you cannot connect to are refused |
| Replication lag | `pg_stat_replication` (primary), `pg_last_wal_receive_lsn()` (standby) | `pg_monitor` for the standby detail | `pg_is_in_recovery()` first: it tells you which side you are on |
| Top statements: calls, total/mean time, rows | `pg_stat_statements` | **the extension**, plus `pg_read_all_stats` to see other users' text | opt-in, see below |

**Masking.** A plain login role reading `pg_stat_activity` gets a row per
backend but `query`, `state` and the wait columns come back as
`<insufficient privilege>` — confirmed on 16. `monitoring.masked_sessions()`
detects exactly that string, and the panel says "other sessions' SQL is
hidden" instead of showing a column of placeholder text. The fix the user
needs is one grant: `GRANT pg_monitor TO <role>` (PostgreSQL 10+).

**pg_stat_statements is opt-in and cannot be turned on from a client.**
The extension ships in `contrib` and is listed in
`pg_available_extensions` on every test server, but it must be in
`shared_preload_libraries`, which needs a config change *and a server
restart*; `CREATE EXTENSION` alone is not enough. None of the test
servers load it, and the probe reports it missing with that reason. So:
the statements panel is offered when it answers, and otherwise explains
the two-step fix rather than hiding.

**Minimum version.** Everything except `pg_stat_io` works unchanged back
to PostgreSQL 10, the floor the metadata layer already sets.

## MySQL

| Metric | Source | Needs | Notes |
| --- | --- | --- | --- |
| Connections, queries, aborts, temp tables, slow count | `SHOW GLOBAL STATUS` | nothing | open to every account |
| Buffer pool hit rate | `Innodb_buffer_pool_read_requests` / `..._reads` | nothing | counters since start — chart deltas |
| Server config for context (`max_connections`, pool size) | `SHOW GLOBAL VARIABLES` | nothing | |
| Sessions: user, host, db, command, time, state, SQL | `SHOW PROCESSLIST` | **PROCESS** to see other accounts | degrades by *silently listing only your own threads* |
| Waits, statement digests, table/index I/O | `performance_schema.*` | SELECT on `performance_schema`, and instrumentation enabled | refuses outright without it — the friendly failure |
| Statement analysis, unused indexes, table stats | `sys.*` | SELECT on `sys` (which reads `performance_schema`) | 5.7+ ships `sys`; the views are just SQL over P_S |
| Engine internals (pending I/O, history list, semaphores) | `SHOW ENGINE INNODB STATUS` | PROCESS | one text blob, not columns — parse sparingly or show verbatim |
| Database and table sizes | `information_schema.tables` | nothing, but only shows visible schemas | see the cost note |
| Replication lag | `SHOW REPLICA/SLAVE STATUS` | REPLICATION CLIENT | `Seconds_Behind_Source` |

**The privilege cliff is sharper than PostgreSQL's.** The unprivileged
`sqlide` fixture account (ALL on its own schemas, USAGE globally) is
refused `performance_schema`, `sys` and replication status outright, and
gets a one-row `SHOW PROCESSLIST` — its own connection — with no error at
all. That last case is why `monitoring.probe()` compares the processlist
length against `Threads_connected` and reports `restricted` when they
disagree. The grant that fixes the whole column is:

```sql
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'user'@'%';
GRANT SELECT ON performance_schema.* TO 'user'@'%';
GRANT SELECT ON sys.* TO 'user'@'%';
```

**Version.** 5.7 and 8.0 behave the same for everything above; 8.0 renames
the replication statement (`SHOW REPLICA STATUS`, with the old spelling
still accepted) and moves `information_schema` onto the data dictionary.
MariaDB is close enough to reuse the same list, but it was not tested.

## Sampling cost

Measured through the real drivers over loopback, per call, on an idle
server (PostgreSQL 16 / MySQL 8):

| Query | Cost |
| --- | --- |
| `pg_stat_activity`, `pg_stat_database`, `pg_stat_bgwriter`, `pg_stat_io`, `pg_locks` | 0.16–0.27 ms each |
| `pg_database_size()` | 0.45 ms |
| Every table's `pg_total_relation_size()` | 1.4 ms |
| `SHOW GLOBAL STATUS` (all ~500 rows) | 1.4 ms |
| `SHOW GLOBAL STATUS LIKE '…'` | 0.25 ms |
| `SHOW PROCESSLIST` | 0.07 ms |
| `information_schema.tables` sizes | 1.9 ms |

A full sample of the live panels is therefore about **1–2 ms of server
work**, which makes a 2-second poll roughly 0.1% of one core. That is
safe on production. The caveats are not the counters:

- **Size queries are the expensive ones and they barely change.** Both
  `pg_total_relation_size()` over every relation and
  `information_schema.tables` scale with the number of tables, and on
  MySQL 5.7 the latter can stat every table file. Poll storage on a
  separate, much slower timer (a minute, or on demand), never with the
  activity panel.
- **`pg_locks` is cheap but taken under a lock-manager lock**; on a
  server already contended it is the one to back off first.
- **One connection, not one per panel.** Monitoring must use a dedicated
  connection so it never interleaves with the user's transaction, and
  must not open a second one per refresh — a poll that connects is
  vastly more expensive than the query it runs.
- **Everything is a counter.** `Questions`, `xact_commit`, `blks_hit`
  and friends are cumulative since startup or since a stats reset. The
  dashboard charts first differences over the poll interval, and must
  cope with a counter going *backwards* (a restart, or
  `pg_stat_reset()`) by starting a new series rather than plotting a
  negative spike.

## Recommendation for CORE-15

**Go.** Scope it as three tiers.

**Ship, always drawn** (nothing beyond a normal login):

1. Sessions list — your rows guaranteed, everyone's where privileged,
   with an explicit banner when it is restricted.
2. Throughput — commits/rollbacks or questions per second, connections
   in use against `max_connections`.
3. Cache hit ratio — `blks_hit`/`blks_read`, or the InnoDB buffer pool
   equivalent.
4. Locks and blocked sessions (PostgreSQL); table locks and waits from
   `SHOW GLOBAL STATUS` (MySQL).
5. Storage — database sizes and the largest tables, on a slow timer.

**Offer where available, explain where not** — every one of these is a
panel that renders a reason, not a blank chart, from
`monitoring.probe()`: `pg_stat_statements`, `pg_stat_io` (16+),
replication, `performance_schema` / `sys`, InnoDB engine status.

**Out of reach, said plainly once:** host CPU, RAM, disk space and disk
I/O. A short line in the dashboard footer — "sqlide reads only what the
server reports over SQL; host metrics need an agent" — is the whole
treatment.

**Kill/cancel.** `pg_cancel_backend` / `pg_terminate_backend` need
`pg_signal_backend` or ownership of the session's role; MySQL's `KILL`
needs `CONNECTION_ADMIN` (8.0) or `SUPER` (5.7) for other accounts'
threads. Check before offering the button, and treat "unknown thread id"
/ `false` as a lost race, not an error — both engines return it for a
session that has already gone.

**Defaults.** Poll live panels every 2 s (configurable 1–60 s, pausable),
storage every 60 s, and stop every timer when the view closes or the
window loses the tab. Keep a rolling window of about 5 minutes of samples
in memory; nothing is persisted.
