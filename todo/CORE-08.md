## CORE-08 — Resizable left sidebar with scrolling

- **Status:** done

### Acceptance criteria

- [x] The left sidebar has a drag handle on its inner edge, like the right one.
- [x] Enforced min and max width; double-clicking the handle resets to default.
- [x] When the tree is taller than the panel, a vertical scrollbar appears.
- [x] When node labels are wider than the panel, a horizontal scrollbar appears
      (labels are not truncated into uselessness); long names get a tooltip.
- [x] Width persists across restarts (via CORE-13 config).
- [x] Resizing stays smooth with a large tree — virtualise the tree if needed.


