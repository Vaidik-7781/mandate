import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig } from "vite";

// Six entry pages from one project: the buyer app (index), the merchant console, and the site pages.
// `npm run build` also makes a server bundle (entry-server.tsx) that scripts/prerender.mjs uses to write real HTML
// into every page, so crawlers and slow connections get content before any JavaScript runs.
const pages = ["index", "console", "about", "contact", "privacy", "404"];

export default defineConfig(({ isSsrBuild }) => ({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000", "/health": "http://127.0.0.1:8000" } },
  build: isSsrBuild
    ? { outDir: "dist-ssr", emptyOutDir: true }
    : { chunkSizeWarningLimit: 950, /* the lazy AG Grid chunk */ rollupOptions: { input: Object.fromEntries(pages.map((p) => [p, resolve(__dirname, `${p}.html`)])) } },
}));
