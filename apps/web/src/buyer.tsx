import { useEffect, useState } from "react";
import { api, Json, Preset, short, usd, when } from "./api";
import { Err, Footer, Guilloche, Header, RealityPanel, Seal, SealKind, useApi, Walkthrough } from "./common";

const SAMPLE = "Buy me black running shoes under $120, max 2 pairs, only from RunFast or TrailCo, valid for 7 days";
const FALLBACK_PRESET: Preset = { id: "runner", title: "Running shoes", audience: "", story: "", prompt: SAMPLE, hijack_hint: "" };

type Slip = { id: number; kind: "allow" | "deny" | "step_up"; title: string; reasons: string[]; plan?: Json; order?: Json; approveUrl?: string; approvalId?: string; at: number };
const LABEL: Record<Slip["kind"], string> = { allow: "Allowed", deny: "Refused", step_up: "Needs you" };

function Builder({ onMandate }: { onMandate: (m: Json, p: Preset | null) => void }) {
  const presets = useApi<Preset[]>("/api/presets");
  const [preset, setPreset] = useState<Preset | null>(null);
  const [prompt, setPrompt] = useState(SAMPLE);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<Json | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  function pick(p: Preset) {
    setPreset(p);
    setPrompt(p.prompt);
    setAnswers({});
    setDraft(null);
  }
  async function compile() {
    setBusy(true);
    setErr("");
    try {
      setDraft(await api.post("/api/compile", { prompt, answers }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function sign() {
    if (!draft) return;
    setBusy(true);
    setErr("");
    try {
      const m = await api.post("/api/mandates", {
        prompt,
        draft: draft.draft,
        selected_skus: draft.candidates.map((c: Json) => c.sku),
        confirmed: true,
        ambiguity_flagged: draft.ambiguity_flagged,
      });
      onMandate(m, preset);
      setDraft(null);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const d = draft?.draft;
  return (
    <section className="card" aria-labelledby="b-h">
      <h2 id="b-h">What may your agent buy?</h2>
      {(presets.data ?? []).length > 0 && (
        <div className="stack-sm" style={{ marginTop: "var(--s4)" }}>
          <p className="small muted">Start from a situation, or write your own below.</p>
          <div className="personas" role="group" aria-label="Example situations">
            {(presets.data ?? []).map((p) => (
              <button key={p.id} type="button" className="persona" aria-pressed={preset?.id === p.id} onClick={() => pick(p)}>
                <b>{p.title}</b>
                <span>{p.audience}</span>
              </button>
            ))}
          </div>
          {preset && <p className="small">{preset.story}</p>}
        </div>
      )}
      <label className="field">
        Describe it in your own words
        <textarea value={prompt} maxLength={2000} onChange={(e) => { setPrompt(e.target.value); setDraft(null); }} />
      </label>
      <div className="row" style={{ marginTop: "var(--s4)" }}>
        <button className="btn" onClick={compile} disabled={busy || !prompt.trim()}>Turn into limits</button>
      </div>
      <Err>{err}</Err>
      {d && (
        <div className="stack" style={{ marginTop: "var(--s5)" }}>
          {d.questions.length > 0 ? (
            <div className="note stack-sm">
              <p><b>Your agent will not guess.</b> Answer these first.</p>
              {d.questions.map((q: Json) => (
                <label className="field" key={q.field}>
                  {q.question}
                  <input type="text" value={answers[q.field] ?? ""} placeholder={q.options?.join(", ")}
                    onChange={(e) => setAnswers({ ...answers, [q.field]: e.target.value })} />
                </label>
              ))}
              <div><button className="btn" onClick={compile} disabled={busy}>Update limits</button></div>
            </div>
          ) : (
            <>
              <dl className="limits">
                <div><dt>Per purchase</dt><dd>{usd(d.max_per_txn_minor, d.currency)}</dd></div>
                <div><dt>Total budget</dt><dd>{usd(d.budget_minor, d.currency)}</dd></div>
                <div><dt>Max units</dt><dd>{d.max_quantity}</dd></div>
                <div><dt>Valid for</dt><dd>{Math.round(d.valid_seconds / 86400)} days</dd></div>
              </dl>
              <p className="small muted">
                Items: {draft!.candidates.map((c: Json) => `${c.name} (${usd(c.price_minor)})`).join(", ")}. Merchants:{" "}
                {d.merchant_ids.join(", ")}.
              </p>
              <div className="row">
                <button className="btn go" onClick={sign} disabled={busy}>Sign this mandate</button>
                <span className="small muted">Creates a credential only your agent can spend from.</span>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}

function Instrument({ m, onRevoke, onNew, busy }: { m: Json; onRevoke: () => void; onNew: () => void; busy: boolean }) {
  const b = m.balance ?? {};
  const total = b.budget_minor || 1;
  const active = m.status === "active";
  const serial = String(m.chain_digest ?? "");
  return (
    <section className="instrument" aria-labelledby="m-h">
      <div className="band">
        <Guilloche />
        <div className="band-head">
          <div>
            <h2 id="m-h">Your mandate</h2>
            <p className="small muted">{m.merchants.map((x: Json) => x.name).join(" and ")} only</p>
          </div>
          <Seal kind={active ? "verified" : "deny"}>{active ? "Active" : m.status}</Seal>
        </div>
      </div>
      <div className="body">
        <dl className="limits">
          <div><dt>Per purchase</dt><dd>{usd(m.max_per_txn_minor, m.currency)}</dd></div>
          <div><dt>Remaining</dt><dd>{usd(b.remaining_minor, m.currency)}</dd></div>
          <div><dt>Units</dt><dd>{m.max_quantity}</dd></div>
          <div><dt>Expires</dt><dd className="text">{when(m.expires_at)}</dd></div>
        </dl>
        <div className="stack-sm">
          <div className="gauge" role="img" aria-label={`Spent ${usd(b.spent_minor)}, held ${usd(b.reserved_minor)} of ${usd(b.budget_minor)}`}>
            <span className="spent" style={{ width: `${(100 * (b.spent_minor ?? 0)) / total}%` }} />
            <span className="held" style={{ width: `${(100 * (b.reserved_minor ?? 0)) / total}%` }} />
          </div>
          <p className="small muted">Spent {usd(b.spent_minor)}, held {usd(b.reserved_minor)}, of {usd(b.budget_minor)}.</p>
        </div>
        <div className="row between">
          <span className="hash" title="Hash of the signed credential chain">Chain {short(serial, 8)}</span>
          {active ? (
            <button className="btn danger" onClick={onRevoke} disabled={busy}>
              Revoke and refund anything undelivered
            </button>
          ) : (
            <button className="btn quiet" onClick={onNew}>Start a new mandate</button>
          )}
        </div>
        <div className="microprint" aria-hidden="true">{(serial + " ").repeat(8)}</div>
      </div>
    </section>
  );
}

function SlipView({ s, replay, refresh, add }: { s: Slip; replay: boolean; refresh: () => void; add: (s: Slip) => void }) {
  const [order, setOrder] = useState<Json | undefined>(s.order);
  const [err, setErr] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<Json | null>(null);
  const [valid, setValid] = useState<boolean | null>(null);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setErr("");
    try { await fn(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); refresh(); }
  }
  const act = (path: string) => run(async () => {
    const r = await api.post(path);
    setOrder(r.order ?? r);
  });
  async function decide(approve: boolean) {
    await run(async () => {
      const r = await api.post(`/api/approvals/${s.approvalId}/decide`, { approve });
      if (r.order) add({ id: Date.now(), kind: "allow", title: `Approved: ${r.order.items?.[0]?.name ?? "purchase"}`, reasons: ["You approved it, so a fresh credential was signed."], order: r.order, approveUrl: r.approve_url, at: Date.now() });
      else add({ id: Date.now(), kind: "deny", title: "You declined", reasons: ["No purchase was made."], at: Date.now() });
    });
  }
  async function showReceipt() {
    await run(async () => {
      const r = await api.get(`/api/orders/${order!.id}/receipt`);
      setReceipt(r);
      setValid((await api.post("/api/receipts/verify", r)).valid);
    });
  }
  const st = order?.status;
  return (
    <article className={`slip ${s.kind}`}>
      <header>
        <Seal kind={s.kind as SealKind}>{LABEL[s.kind]}</Seal>
        <span className="time">{new Date(s.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      </header>
      <h3>{s.title}</h3>
      <ul>{s.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      {s.plan?.rationale && <p className="small muted">Agent said: {s.plan.rationale}</p>}
      {order && (
        <div className="chips" aria-label="Order">
          <span className="chip strong">{usd(order.amount_minor, order.currency)}</span>
          <span className="chip">{st}</span>
          {order.tracking && <span className="chip">{order.tracking.carrier} {order.tracking.tracking_number}</span>}
          <span className="chip">chain {short(order.chain_digest, 6)}</span>
        </div>
      )}
      {s.kind === "step_up" && s.approvalId && (
        <div className="actions">
          <button className="btn go" disabled={busy} onClick={() => decide(true)}>Approve this purchase</button>
          <button className="btn quiet" disabled={busy} onClick={() => decide(false)}>Decline</button>
        </div>
      )}
      {order && (
        <div className="actions">
          {st === "CREATED" && replay && <button className="btn" disabled={busy} onClick={() => act(`/api/orders/${order.id}/simulate-approval`)}>Approve in simulated PayPal</button>}
          {st === "CREATED" && !replay && s.approveUrl && <a className="btn" href={s.approveUrl} target="_blank" rel="noreferrer">Approve in PayPal</a>}
          {st === "APPROVED" && <button className="btn go" disabled={busy} onClick={() => act(`/api/orders/${order.id}/capture`)}>Capture payment</button>}
          {st === "CAPTURED" && !order.delivered_at && <button className="btn" disabled={busy} onClick={() => act(`/api/orders/${order.id}/deliver`)}>Mark delivered</button>}
          {(st === "CAPTURED" || st === "REFUNDED") && <button className="btn quiet" disabled={busy} onClick={showReceipt}>Signed receipt</button>}
          {st === "CAPTURED" && replay && (
            <button className="btn quiet" disabled={busy} onClick={() => run(async () => {
              const r = await api.post(`/api/orders/${order.id}/sim-dispute`, { message: "My parcel never arrived." });
              setInfo(`Dispute ${r.dispute_id} opened. Review it in the Merchant console.`);
            })}>Open a dispute</button>
          )}
        </div>
      )}
      {info && <p className="info" role="status">{info}</p>}
      <Err>{err}</Err>
      {receipt && (
        <p className="proof">
          {valid ? <Seal kind="verified">Signature valid.</Seal> : <Seal kind="bad">Signature invalid.</Seal>}{" "}
          Receipt for {usd(receipt.receipt.amount_minor)} under mandate <span className="hash">{short(receipt.receipt.mandate_id, 8)}</span>.{" "}
          <a href={`/api/orders/${order!.id}/receipt.pdf`} target="_blank" rel="noreferrer">Open PDF</a>
        </p>
      )}
    </article>
  );
}

function App() {
  const cfg = useApi("/api/config");
  const [mandate, setMandate] = useState<Json | null>(null);
  const [preset, setPreset] = useState<Preset | null>(null);
  const [slips, setSlips] = useState<Slip[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [query, setQuery] = useState("");
  const [qty, setQty] = useState(1);

  // Remember the mandate for this tab only, so moving to the console and back does not lose it.
  const KEY = "mandate.id";
  const remember = (id: string | null) => {
    try { if (id) sessionStorage.setItem(KEY, id); else sessionStorage.removeItem(KEY); } catch { /* storage may be unavailable */ }
  };
  useEffect(() => {
    let id: string | null = null;
    try { id = sessionStorage.getItem(KEY); } catch { /* ignore */ }
    if (id) api.get(`/api/mandates/${id}`).then(setMandate).catch(() => remember(null));
  }, []);
  async function refresh() {
    if (mandate) setMandate(await api.get(`/api/mandates/${mandate.id}`));
  }
  useEffect(() => { void refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [slips.length]);
  const add = (s: Slip) => setSlips((x) => [s, ...x]);

  async function purchase(hijacked: boolean) {
    if (!mandate) return;
    setBusy(true);
    setErr("");
    try {
      const r = await api.post(`/api/mandates/${mandate.id}/purchase`, { hijacked, quantity: qty, query: query || null });
      const dec = r.decision ?? {};
      const item = r.plan?.chosen_sku ?? "item";
      if (r.outcome === "created") add({ id: Date.now(), kind: "allow", title: `Agent bought ${r.order.items?.[0]?.name ?? item}`, reasons: dec.reasons ?? [], plan: r.plan, order: r.order, approveUrl: r.approve_url, at: Date.now() });
      else if (r.outcome === "step_up") add({ id: Date.now(), kind: "step_up", title: `Agent wants ${item} x${r.plan?.quantity ?? 1}`, reasons: dec.reasons?.length ? dec.reasons : ["Text from the web influenced this choice."], plan: r.plan, approvalId: r.approval_id, at: Date.now() });
      else if (r.outcome === "no_candidate") add({ id: Date.now(), kind: "deny", title: "Nothing matched", reasons: ["No item inside your limits matches."], at: Date.now() });
      else add({ id: Date.now(), kind: "deny", title: `Agent tried ${item} x${r.plan?.quantity ?? 1}`, reasons: dec.reasons ?? [r.error ?? "Refused"], plan: r.plan, at: Date.now() });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function revoke() {
    if (!mandate) return;
    setBusy(true);
    try {
      const r = await api.post(`/api/mandates/${mandate.id}/revoke`, {});
      add({ id: Date.now(), kind: "deny", title: "You revoked the mandate", reasons: [`Cancelled ${r.cancelled_orders?.length ?? 0} open order(s), refunded ${r.refunded_orders?.length ?? 0}.`], at: Date.now() });
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Header page="buyer" cfg={cfg.data} />
      <main className="shell">
        <section className="hero">
          <h1>Let your agent pay. Keep the proof.</h1>
          <p>
            You sign what your agent may buy. Every purchase is checked against it before money moves, and every
            decision is written to a log nobody can quietly edit.
          </p>
        </section>
        <Walkthrough step={!mandate ? 0 : slips.length === 0 ? 1 : (mandate.balance?.spent_minor ?? 0) > 0 ? 3 : 2} />
        <div className="grid">
          <div className="stack">
            {mandate ? <Instrument m={mandate} onRevoke={revoke} onNew={() => { remember(null); setMandate(null); setSlips([]); setPreset(null); }} busy={busy} /> : <Builder onMandate={(m, p) => { remember(m.id); setMandate(m); setPreset(p ?? FALLBACK_PRESET); }} />}
            {mandate && (
              <section className="card" aria-labelledby="a-h">
                <h2 id="a-h">Send your agent shopping</h2>
                <div className="row" style={{ alignItems: "end" }}>
                  <label className="field" style={{ flex: 1, minWidth: 160 }}>
                    Search
                    <input type="text" value={query} placeholder="running shoes" onChange={(e) => setQuery(e.target.value)} />
                  </label>
                  <label className="field" style={{ width: 96 }}>
                    Units
                    <input type="number" min={1} max={9} value={qty} onChange={(e) => setQty(Math.max(1, Number(e.target.value) || 1))} />
                  </label>
                </div>
                <div className="row" style={{ marginTop: "var(--s4)" }}>
                  <button className="btn go" onClick={() => purchase(false)} disabled={busy || mandate.status !== "active"}>Buy for me</button>
                  <button className="btn quiet" onClick={() => purchase(true)} disabled={busy || mandate.status !== "active"}>
                    Simulate a hijacked agent
                  </button>
                </div>
                <p className="small muted" style={{ marginTop: "var(--s3)" }}>
                  {preset?.hijack_hint || "The catalog contains reviews with hidden instructions."} The hijacked agent obeys them; Mandate should not.
                </p>
                <Err>{err}</Err>
              </section>
            )}
          </div>
          <section aria-labelledby="s-h">
            <h2 id="s-h" className="section-title">Decision slips</h2>
            {slips.length === 0 ? (
              <p className="empty">
                <strong>Nothing to review yet.</strong>
                Sign a mandate, then send your agent shopping. Each attempt lands here with the reason it was allowed or refused.
              </p>
            ) : (
              <div className="slips" aria-live="polite">
                {slips.map((s) => (
                  <SlipView key={s.id} s={s} replay={!!cfg.data?.replay} refresh={refresh} add={add} />
                ))}
              </div>
            )}
          </section>
        </div>
        <RealityPanel cfg={cfg.data} />
        <Footer cfg={cfg.data} />
      </main>
    </>
  );
}

export { App as BuyerApp };
