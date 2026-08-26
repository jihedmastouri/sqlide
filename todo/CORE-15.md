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

