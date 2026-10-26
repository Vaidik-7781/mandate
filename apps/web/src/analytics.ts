// Google Analytics 4, loaded only when the server is configured with a measurement id AND the visitor accepted.
// No id: nothing here ever runs. No consent: no script is requested and no cookie is set.
const KEY = "mandate.analytics";
const ID_FORMAT = /^G-[A-Z0-9]{4,20}$/;

export type Choice = "granted" | "denied" | null;

export function storedChoice(): Choice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "granted" || v === "denied" ? v : null;
  } catch {
    return null; // storage can be blocked; treat as undecided and ask again
  }
}

export function saveChoice(c: Exclude<Choice, null> | "reset"): void {
  try {
    if (c === "reset") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, c);
  } catch {
    /* ignore */
  }
}

export const validId = (id: unknown): id is string => typeof id === "string" && ID_FORMAT.test(id);

let loaded = false;
export function loadAnalytics(id: string): void {
  if (loaded || !validId(id)) return;
  loaded = true;
  const w = window as unknown as { dataLayer: unknown[]; gtag: (...a: unknown[]) => void };
  w.dataLayer = w.dataLayer || [];
  w.gtag = function () {
    // eslint-disable-next-line prefer-rest-params
    w.dataLayer.push(arguments);
  };
  w.gtag("js", new Date());
  w.gtag("config", id, { anonymize_ip: true });
  const s = document.createElement("script");
  s.async = true;
  s.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(id)}`;
  document.head.appendChild(s);
}
