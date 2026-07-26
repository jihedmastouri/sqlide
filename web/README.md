# sqlide website

The product page and documentation site, built with [Astro](https://astro.build).
Docs content lives as plain Markdown in [`../docs`](../docs) and is pulled in
via an Astro content collection (`src/content.config.ts`) — edit the `.md`
files there, not anything under `src/`.

```sh
npm install
npm run dev      # http://localhost:4321
npm run build    # outputs to dist/
npm run preview  # serve the production build locally
```
