## CORE-34 — Export a chart as PNG or SVG, and copy it

- **Status:** todo
- **Depends on:** CORE-31, CORE-32
- **From:** RS-03 (see `docs/charting-research.md`)

### Problem

A chart that cannot leave the app is half a feature: the reason people
chart a query is to put the picture in a ticket, a slide or a message.
The grid already exports and copies its rows in five formats
(`data_grid.py:87`); the chart exports nothing.

### Goal

Save the chart as PNG or SVG, and copy it to the clipboard, at a size
the user picks.

### Approach

- Because CORE-31's drawing is a pure function of (spec, data, context,
  size), export is the same call against a `cairo.ImageSurface` or
  `cairo.SVGSurface`. No second rendering path — that was one of the
  reasons cairo won over a charting library.
- A save dialog with PNG/SVG and a width/height (and a scale factor for
  PNG, so a 2× image is available for slides). Defaults come from the
  on-screen size.
- Clipboard copy puts a PNG on the clipboard via `Gdk.Texture`.
- Export renders on the **light** palette by default with a checkbox to
  use the current theme, because a dark-background PNG pasted into a
  white document is the usual complaint.
- The exported picture includes the legend and axis labels regardless
  of what the on-screen size had room for.

### Acceptance criteria

- [ ] PNG and SVG export produce a file matching the on-screen chart,
      at the requested size, with legend and axes.
- [ ] The SVG has real vector text, not paths of a bitmap.
- [ ] Copy puts an image on the clipboard that pastes into another app.
- [ ] Theme choice is honoured and defaults to light.
- [ ] A failed write reports the path and the reason, and does not
      leave a partial file.
