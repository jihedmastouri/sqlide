## CORE-14 — Spike: resource & usage monitoring feasibility (PG + MySQL)

- **Status:** todo
- **Blocks:** CORE-15
- **Timebox:** short — this is the "if yes, do it" from the TODO.

### Question

Can we show useful resource/usage monitoring for Postgres and MySQL using only a
normal client connection, and what does it require from the user?

### To answer

- **Postgres:** what's available from `pg_stat_activity`, `pg_stat_database`,
  `pg_stat_bgwriter`/`pg_stat_io`, `pg_stat_statements` (extension — required or
  optional?), `pg_locks`, database/table sizes, cache hit ratio, replication lag.
- **MySQL:** `SHOW GLOBAL STATUS`, `SHOW PROCESSLIST`, `performance_schema`,
  `sys` schema, InnoDB buffer pool stats, slow query counters.
- Which of these need elevated privileges, and what degrades when they're absent.
- Host-level metrics (CPU/RAM/disk) are generally *not* reachable over SQL —
  confirm and decide whether to say so in the UI rather than fake it.
- Polling cost: what interval is safe on a busy production server.

### Deliverable

- [ ] A written recommendation: which metrics ship, which are opt-in, which are
      out of reach.
- [ ] Required privileges per engine, documented.
- [ ] A go/no-go on CORE-15 with a scoped metric list.

