import { defineConfig } from "astro/config";

// Deploy target. Both are set by the Pages workflow:
//
//   SITE=https://<user>.github.io  BASE=/sqlide/  npm run build
//
// and both fall back to a root-hosted site for `npm run dev`.
export default defineConfig({
  site: process.env.SITE ?? "https://jihedmastouri.github.io",
  base: process.env.BASE ?? "/",
  trailingSlash: "always",
});
