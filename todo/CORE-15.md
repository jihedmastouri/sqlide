## CORE-15 — Resource & usage monitoring dashboard

- **Status:** todo
- **Depends on:** CORE-14

### Acceptance criteria (provisional — finalise from the spike)

- [ ] A monitoring view per connection, reachable from the connection node.
- [ ] Live activity: current sessions/processes with user, database, state,
      duration and query, refreshable and sortable.
- [ ] Ability to cancel or kill a session, with confirmation and the privileges
      checked first.
- [ ] Throughput/health charts over a rolling window (connections, transactions
      or queries per second, cache hit ratio, locks/waits).
- [ ] Storage: database and largest-table sizes.
- [ ] Polling interval is configurable and pausable; monitoring stops when the
      view is closed.
- [ ] Missing privileges or extensions produce an explicit "not available because
      X" panel, never a blank chart.


### Notes from CORE-14

The spike is `docs/monitoring-spike.md`; read it before scoping this.
What CORE-15 can now assume:

- **Go, with a scoped tier list.** Always-drawn: sessions, throughput,
  cache hit ratio, locks/waits, storage. Conditional (draw a reason,
  never a blank chart): `pg_stat_statements`, `pg_stat_io` (PG 16+),
  replication, `performance_schema`/`sys`, `SHOW ENGINE INNODB STATUS`.
  Out of reach and to be said once in words: host CPU, RAM, disk.
- **`sqlide/backend/db/monitoring.py` already answers "what can this
  connection read".** `probe(kind, connector)` returns a `SourceStatus`
  per panel — `available`, `restricted`, and a `detail` string written
  for the "not available because X" panel. It never raises. Call it once
  when the view opens, from a worker thread, and let it drive which
  panels exist. `sources("sqlite") == ()` is how the connection node
  knows not to offer monitoring at all.
- **The dangerous failure is silence, not errors.** PostgreSQL masks
  other sessions' SQL as `<insufficient privilege>`; MySQL's
  `SHOW PROCESSLIST` lists only your own threads without PROCESS. Both
  look like an idle server. `restricted` is set for exactly these, and
  the banner must be shown when it is.
- **Polling.** A full live sample is 1–2 ms of server work: 2 s default
  (configurable 1–60 s, pausable) is safe on production. Storage is the
  expensive query and barely moves — put it on a 60 s timer of its own.
  Use one dedicated connection, kept open, never one per refresh.
- **Every metric is a cumulative counter.** Chart first differences, and
  restart the series when a counter goes backwards (server restart or
  `pg_stat_reset()`) instead of plotting a negative spike.
- **Kill/cancel privileges** to check before offering the button:
  `pg_signal_backend` (or owning the role) on PostgreSQL,
  `CONNECTION_ADMIN`/`SUPER` on MySQL. "Unknown thread id" and a `false`
  return are lost races, not failures.
