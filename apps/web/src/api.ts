export type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

export type RealityRow = { part: string; status: string; detail: string };
export type Config = {
  mode: "replay" | "sandbox";
  replay: boolean;
  paypal_client_id: string;
  paypal_env: string;
  llm: string;
  reality: RealityRow[];
  vi_commit: string;
  currency: string;
  disclaimer: string;
};
export type Preset = { id: string; title: string; audience: string; story: string; prompt: string; hijack_hint: string };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** One readable sentence for a FastAPI 422 body: `{detail: [{loc, msg}]}`. */
function validationMessage(detail: unknown[]): string {
  const parts = detail.slice(0, 3).map((d) => {
    const item = d as Json;
    const loc = Array.isArray(item.loc) ? item.loc.filter((x: unknown) => x !== "body").join(".") : "";
    return `${loc ? `${loc}: ` : ""}${item.msg ?? "invalid value"}`;
  });
  return `The server rejected that input (${parts.join("; ")}). Check the fields and try again.`;
}

function failure(res: Response, data: unknown): string {
  const detail = (data as Json)?.detail;
  if (res.status === 429) {
    const wait = Number(res.headers.get("Retry-After")) || 0;
    return `Too many requests from this browser. ${wait ? `Wait about ${wait} seconds` : "Wait a moment"} and try again.`;
  }
  if (res.status === 422 && Array.isArray(detail)) return validationMessage(detail);
  if (res.status === 413) return "That request was too large for the server.";
  if (typeof detail === "string" && detail) return detail;
  if (res.status >= 500) return `The server had a problem (${res.status}). Try again in a moment.`;
  return `Request failed (${res.status}).`;
}

async function call<T = Json>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
  }
  const text = await res.text();
  let data: unknown = text;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    /* keep the raw text */
  }
  if (!res.ok) throw new ApiError(res.status, failure(res, data));
  return data as T;
}

export const api = {
  get: <T = Json>(p: string) => call<T>("GET", p),
  post: <T = Json>(p: string, b: unknown = {}) => call<T>("POST", p, b),
};

export const usd = (minor: number | null | undefined, cur = "USD") =>
  minor == null
    ? "n/a"
    : new Intl.NumberFormat("en-US", { style: "currency", currency: cur }).format(minor / 100);

export const short = (s: string | null | undefined, n = 10) => (s ? (s.length > n * 2 ? `${s.slice(0, n)}…${s.slice(-4)}` : s) : "");

export const when = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
