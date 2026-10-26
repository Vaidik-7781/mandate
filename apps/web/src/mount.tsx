import { StrictMode } from "react";
import type { ReactNode } from "react";
import { createRoot, hydrateRoot } from "react-dom/client";

/** Hydrate the prerendered HTML when it is there; otherwise render from scratch (dev server). */
export function mount(node: ReactNode): void {
  const el = document.getElementById("root")!;
  const tree = <StrictMode>{node}</StrictMode>;
  if (el.hasChildNodes()) hydrateRoot(el, tree);
  else createRoot(el).render(tree);
}
