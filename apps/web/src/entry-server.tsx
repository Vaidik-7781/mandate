import { renderToString } from "react-dom/server";
import { BuyerApp } from "./buyer";
import { ConsoleApp } from "./console";
import { SitePage } from "./site";

/** Build-time prerender: the HTML a crawler (or a visitor with slow JavaScript) receives. */
export function render(page: string): string {
  if (page === "buyer") return renderToString(<BuyerApp />);
  if (page === "console") return renderToString(<ConsoleApp />);
  return renderToString(<SitePage page={page} />);
}
