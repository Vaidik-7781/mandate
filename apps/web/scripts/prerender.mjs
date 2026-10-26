// Writes server-rendered markup into each built page: dist/<page>.html gets the output of render(<page>).
import { readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const ssr = join(root, "dist-ssr");
const entry = ["entry-server.js", "entry-server.mjs"].map((f) => join(ssr, f)).find(existsSync);
if (!entry) throw new Error("dist-ssr/entry-server.js not found: run `vite build --ssr src/entry-server.tsx` first");
const { render } = await import(pathToFileURL(entry).href);

const pages = { index: "buyer", console: "console", about: "about", contact: "contact", privacy: "privacy", 404: "404" };
for (const [file, name] of Object.entries(pages)) {
  const path = join(root, "dist", `${file}.html`);
  const html = readFileSync(path, "utf8");
  const marker = /<div id="root"([^>]*)><\/div>/;
  if (!marker.test(html)) throw new Error(`${file}.html has no empty #root to fill`);
  writeFileSync(path, html.replace(marker, (_m, attrs) => `<div id="root"${attrs}>${render(name)}</div>`));
  console.log(`prerendered ${file}.html`);
}

rmSync(ssr, { recursive: true, force: true });
