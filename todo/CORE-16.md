## CORE-16 — Graphical Explain

- **Status:** done

### Acceptance criteria (provisional — finalise from the spike)

- If using explain the result should enable the option to see the result as
graphs like the one used in entity-relation diagram

### Notes

- The plan view already shipped in `sqlide/frontend/plan_graph.py`:
  `parse_plan()` turns the three shapes EXPLAIN answers in into
  `PlanNode` trees (SQLite's id/parent rows, PostgreSQL's and MySQL
  `FORMAT=TREE`'s indented text, MySQL's classic row-per-table
  pipeline), with a one-node-per-row fallback so the view always has
  something to draw; `PlanGraph` renders it on the same cairo canvas
  primitives as the relation diagram (`frontend/canvas.py`), with the
  same zoom steps, palette and dark/light handling.
- Explain result tabs offer Graph / Table / JSON over one `Gtk.Stack`,
  the graph leading and dropped only when the rows carry no readable
  plan.
- This ticket closed the two gaps left: the parsing had no tests, and
  the graph appeared only for runs started from the Explain button.
  `_is_explain()` in `frontend/query_console.py` now also recognises a
  statement the user typed as an `EXPLAIN`/`DESCRIBE` (leading `--` and
  `/* */` comments skipped), so a hand written plan request gets the
  same three views.
- Choice: recognition is by leading keyword rather than by asking the
  adapter, so it needs no new connector API and works for engines whose
  explain syntax we do not special-case.
