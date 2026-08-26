## CORE-05 — Deep-link from the sidebar into a properties section

- **Status:** todo
- **Depends on:** CORE-04

### Goal

Opening e.g. *Tables → orders → Indexes* from the sidebar lands directly on that
table's Properties view with the Indexes section selected.

### Acceptance criteria

- [ ] Every child node under a table in the sidebar maps to a properties section.
- [ ] If the table tab is already open, it is reused and switched to Properties.
- [ ] The target section is scrolled into view and visibly selected.
- [ ] Opening a single child object (one specific index) opens that object's info
      view instead, per CORE-01.

