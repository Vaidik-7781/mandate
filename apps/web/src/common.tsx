import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, Json } from "./api";
import { Choice, loadAnalytics, saveChoice, storedChoice, validId } from "./analytics";
import "./ui.css";

export function useApi<T = Json>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const reload = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api.get<T>(path));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [path]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void reload(); }, [reload, ...deps]);
  return { data, error, loading, reload };
}

/** The seal: a ring that draws once, with a glyph. Always colour + shape + text. */
export type SealKind = "allow" | "deny" | "step_up" | "verified" | "ok" | "bad" | "hold";
const GLYPH: Record<SealKind, string> = {
  allow: "M7.5 12.4l3 3 6-6.8",
  ok: "M7.5 12.4l3 3 6-6.8",
  verified: "M7.5 12.4l3 3 6-6.8",
  deny: "M8.5 8.5l7 7M15.5 8.5l-7 7",
  bad: "M8.5 8.5l7 7M15.5 8.5l-7 7",
  step_up: "M12 7.5v5.2M12 16.2v.1",
  hold: "M12 7.5v5.2M12 16.2v.1",
};
export function Seal({ kind, children }: { kind: SealKind; children: ReactNode }) {
  return (
    <span className={`seal ${kind}`}>
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle className="ring" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="1.6" transform="rotate(-90 12 12)" />
        <path className="glyph" d={GLYPH[kind]} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      {children}
    </span>
  );
}

/** Guilloche rosette: the engraved pattern on banknotes and certificates. Generated, not an asset. */
export function Guilloche() {
  const paths = useMemo(() => {
    const out: string[] = [];
    const R = 64, r = 23;
    for (let k = 0; k < 7; k++) {
      const d = 18 + k * 7;
      let p = "";
      for (let i = 0; i <= 420; i++) {
        const t = (i / 420) * Math.PI * 2 * 23;
        const x = (R - r) * Math.cos(t) + d * Math.cos(((R - r) / r) * t);
        const y = (R - r) * Math.sin(t) - d * Math.sin(((R - r) / r) * t);
        p += `${i === 0 ? "M" : "L"}${(x * 0.85 + 430).toFixed(1)} ${(y * 0.85 + 56).toFixed(1)}`;
      }
      out.push(p);
    }
    return out;
  }, []);
  return (
    <svg viewBox="0 0 560 112" preserveAspectRatio="xMaxYMid slice" aria-hidden="true" focusable="false">
      {paths.map((p, i) => (
        <path key={i} d={p} fill="none" stroke="currentColor" strokeWidth={0.45} opacity={0.35 + (i % 3) * 0.15} />
      ))}
    </svg>
  );
}

export type Page = "buyer" | "console" | "about" | "contact" | "privacy" | "404";

export function Header({ page, cfg }: { page: Page; cfg: Json | null }) {
  return (
    <div className="bar-top">
      <header className="top">
        <a className="brand" href="/" aria-label="Mandate, home">
          <svg viewBox="0 0 28 28" fill="none" aria-hidden="true">
            <circle cx="14" cy="14" r="12.5" stroke="currentColor" strokeWidth="1.4" />
            <circle cx="14" cy="14" r="8.2" stroke="currentColor" strokeWidth="0.8" opacity="0.5" />
            <path d="M9.4 14.4l3.2 3.2 6-6.9" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Mandate
        </a>
        {cfg && (
          <span className={`mode ${cfg.replay ? "" : "live"}`}>
            {cfg.replay ? "Replay mode: simulated PayPal" : `PayPal ${cfg.paypal_env}`} · {cfg.llm === "offline" ? "offline rules" : cfg.llm}
          </span>
        )}
        <nav aria-label="Main">
          <a href="/" aria-current={page === "buyer" ? "page" : undefined}>Buyer app</a>
          <a href="/console.html" aria-current={page === "console" ? "page" : undefined}>Merchant console</a>
          <a href="/about" aria-current={page === "about" ? "page" : undefined}>About</a>
          <a href="/contact" aria-current={page === "contact" ? "page" : undefined}>Contact</a>
        </nav>
      </header>
    </div>
  );
}

export function Footer({ cfg }: { cfg: Json | null }) {
  const [choice, setChoice] = useState<Choice>(null);
  const [ready, setReady] = useState(false);
  const gaId = cfg?.ga_id;
  useEffect(() => {
    setChoice(storedChoice());
    setReady(true);
  }, []);
  useEffect(() => {
    if (validId(gaId) && choice === "granted") loadAnalytics(gaId);
  }, [gaId, choice]);
  const decide = (c: "granted" | "denied") => {
    saveChoice(c);
    setChoice(c);
  };
  return (
    <>
      <footer className="foot" aria-label="Site">
        <nav className="foot-nav" aria-label="Footer">
          <a href="/">Buyer app</a>
          <a href="/console.html">Merchant console</a>
          <a href="/about">About</a>
          <a href="/contact">Contact</a>
          <a href="/privacy">Privacy</a>
          <a href="/docs">API docs</a>
          <a href="/sitemap.xml">Sitemap</a>
          {validId(gaId) && choice !== null && (
            <button type="button" className="linklike" onClick={() => { saveChoice("reset"); setChoice(null); }}>
              Analytics choices
            </button>
          )}
        </nav>
        <p>
          {cfg?.disclaimer ?? "Sandbox only. Not production-ready, not legal advice, not endorsed by PayPal."} The
          PayPal binding of Verifiable Intent is a proposal (draft v0.1, commit {String(cfg?.vi_commit ?? "").slice(0, 7)}).
          Dispute outcomes in the sandbox are scripted; the benchmark is synthetic.
        </p>
      </footer>
      {ready && validId(gaId) && choice === null && (
        <div className="consent" role="dialog" aria-label="Analytics choice" aria-live="polite">
          <p>
            May we count visits with Google Analytics? Nothing loads unless you accept. <a href="/privacy">Privacy</a>
          </p>
          <div className="actions">
            <button type="button" className="btn go" onClick={() => decide("granted")}>Accept</button>
            <button type="button" className="btn quiet" onClick={() => decide("denied")}>Decline</button>
          </div>
        </div>
      )}
    </>
  );
}

export function Breadcrumbs({ trail }: { trail: [string, string | null][] }) {
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      <ol>
        {trail.map(([label, href], i) => (
          <li key={label} aria-current={i === trail.length - 1 ? "page" : undefined}>
            {href ? <a href={href}>{label}</a> : label}
          </li>
        ))}
      </ol>
    </nav>
  );
}

const tagClass = (status: string) => (status === "real code" || status === "real sandbox" || status === "live model" || status === "PayPal approve page" ? "real" : "sim");

/** Plain statement of what the viewer is looking at: which parts are real and which are simulated. */
export function RealityPanel({ cfg }: { cfg: Json | null }) {
  const rows: { part: string; status: string; detail: string }[] = cfg?.reality ?? [];
  if (rows.length === 0) return null;
  return (
    <details className="reality">
      <summary>What is real in this demo, and what is simulated</summary>
      <div className="scroll">
        <table>
          <thead><tr><th>Part</th><th>This run</th><th>Detail</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.part}><td>{r.part}</td><td><span className={`tag ${tagClass(r.status)}`}>{r.status}</span></td><td className="small muted">{r.detail}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

export function Walkthrough({ step }: { step: number }) {
  const items = [
    ["Sign", "Describe what the agent may buy"],
    ["Shop", "The agent tries; the guard decides"],
    ["Pay", "Approve, capture, get a signed receipt"],
    ["Dispute", "Evidence, rule, human gate"],
  ];
  return (
    <ol className="steps" aria-label="Walkthrough">
      {items.map(([t, d], i) => (
        <li key={t} className={i < step ? "done" : ""} aria-current={i === step ? "step" : undefined}>
          <b>{t}</b>
          <span>{d}</span>
        </li>
      ))}
    </ol>
  );
}

export function Err({ children }: { children?: string }) {
  return children ? <p className="err" role="alert">{children}</p> : null;
}

export function Split({ split }: { split: { user: number; agent_platform: number; merchant: number } }) {
  return (
    <div className="split" role="img" aria-label={`Fault: user ${split.user}%, agent platform ${split.agent_platform}%, merchant ${split.merchant}%`}>
      <i className="user" style={{ width: `${split.user}%` }} />
      <i className="platform" style={{ width: `${split.agent_platform}%` }} />
      <i className="merchant" style={{ width: `${split.merchant}%` }} />
    </div>
  );
}

export function SplitLegend() {
  return (
    <p className="legend">
      <span><b style={{ background: "var(--hold)" }} />User</span>
      <span><b style={{ background: "var(--refused)" }} />Agent platform</span>
      <span><b style={{ background: "var(--seal)" }} />Merchant</span>
    </p>
  );
}
