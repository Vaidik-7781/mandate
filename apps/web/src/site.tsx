import { FormEvent, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import { Breadcrumbs, Err, Footer, Header, Page, RealityPanel, useApi } from "./common";

function Shell({ page, trail, children }: { page: Page; trail?: [string, string | null][]; children: ReactNode }) {
  const cfg = useApi("/api/config");
  return (
    <>
      <Header page={page} cfg={cfg.data} />
      <main className="shell">
        {trail && <Breadcrumbs trail={trail} />}
        {children}
        <Footer cfg={cfg.data} />
      </main>
    </>
  );
}

function About() {
  const cfg = useApi("/api/config");
  return (
    <Shell page="about" trail={[["Home", "/"], ["About", null]]}>
      <section className="hero">
        <h1>Agents can pay. Mandate lets them prove they were allowed to.</h1>
        <p>
          An open-source accountability layer for AI-agent purchases on PayPal: you sign what your agent may buy, plain
          code checks every purchase against it before money moves, and a disputed payment is answered from evidence
          and published rules.
        </p>
      </section>
      <div className="prose">
        <h2>The problem</h2>
        <p>
          An AI agent with a payment method is a confused deputy. A line of text in a product review can talk it into
          spending someone's money, and when that payment is disputed nobody can show what the person actually allowed
          or who is at fault.
        </p>
        <h2>What Mandate does</h2>
        <ul>
          <li><b>Prove.</b> You sign a Verifiable Intent credential chain. The PayPal order carries the hash of that chain, and receipts are signed.</li>
          <li><b>Protect.</b> A guard in plain code checks the chain, every limit, the merchant, the cart and a budget ledger. A quarantined extractor keeps web text away from the planner.</li>
          <li><b>Resolve.</b> A dispute agent applies a published rule set, assembles an evidence packet and decides refund, defend or human, with a money gate.</li>
          <li><b>Attribute.</b> Fault is split between the user, the agent platform and the merchant by deterministic rules. A model may explain the result; it cannot change it.</li>
        </ul>
        <p>
          The rule that holds it together: <b>the model proposes, code disposes</b>. Money only moves through a gateway
          that demands a token the guard issues.
        </p>
      </div>
      <div className="figs">
        <figure>
          <img src="/img/buyer.webp" width="1200" height="760" loading="lazy" decoding="async"
            alt="The Mandate buyer app: a signed mandate with a per-purchase limit and remaining budget, next to decision slips showing one allowed purchase with a signed receipt and one refused purchase with the reason." />
          <figcaption>The buyer app: a signed mandate, and the agent's attempts with the reason for each decision.</figcaption>
        </figure>
        <figure>
          <img src="/img/console.webp" width="1200" height="760" loading="lazy" decoding="async"
            alt="The Mandate merchant console: a table of guard decisions with verdict, item, units and untrusted-input flags, above a summary of orders, captured volume and an intact audit chain." />
          <figcaption>The merchant console: guard decisions, orders, disputes and a verifiable audit log.</figcaption>
        </figure>
      </div>
      <div className="prose" style={{ marginTop: "var(--s6)" }}>
        <h2>What is real and what is simulated</h2>
        <p>
          Nothing is oversold here. PayPal is an in-process simulator unless sandbox credentials are set, dispute
          outcomes are scripted, and the benchmark uses synthetic scenarios. The guard, ledger, credentials, liability
          rules, receipts and audit log are real code in every mode.
        </p>
      </div>
      <RealityPanel cfg={cfg.data} />
      <div className="prose" style={{ marginTop: "var(--s6)" }}>
        <h2>Where to go next</h2>
        <ul>
          <li><a href="/">Try the buyer app</a>: sign a mandate and send an agent shopping.</li>
          <li><a href="/console.html">Open the merchant console</a>: orders, guard decisions, disputes, audit log, benchmark.</li>
          <li><a href="/docs">Read the API reference</a>, including the MCP endpoint for other agents.</li>
          <li><a href="/contact">Get in touch</a> with questions or feedback.</li>
        </ul>
        <p className="small muted">Sandbox project. Not production-ready, not legal advice, not endorsed by PayPal.</p>
      </div>
    </Shell>
  );
}

function Privacy() {
  return (
    <Shell page="privacy" trail={[["Home", "/"], ["Privacy", null]]}>
      <section className="hero">
        <h1>Privacy policy</h1>
        <p>What this site stores, why, and how to ask for it to be removed. Plain language, no tracking by default.</p>
      </section>
      <div className="prose">
        <p className="small muted">
          This is a sandbox demonstration, not a commercial service. This page describes how the software behaves; it is
          not legal advice, and an operator who deploys it should review it against the laws that apply to them.
        </p>
        <h2>What we collect</h2>
        <ul>
          <li><b>Demo activity.</b> Mandates, orders, approvals, disputes and audit entries you create while using the demo. They are sandbox records with no real payment data. Do not type real personal or card details into any field.</li>
          <li><b>Contact messages.</b> If you use the contact form we store your name, email address, optional subject and your message so we can reply. Your network address is not stored; only a salted one-way hash used to limit spam.</li>
          <li><b>Server logs.</b> The hosting setup may keep standard request logs (time, path, status) for security and debugging.</li>
        </ul>
        <h2>Cookies and similar storage</h2>
        <p>
          The site sets no cookies. Your browser's session storage remembers the id of the mandate you are working with
          in that tab, and local storage remembers your analytics choice. Both stay on your device.
        </p>
        <h2>Analytics</h2>
        <p>
          Analytics are off unless the operator configures Google Analytics. When they do, nothing loads until you press
          Accept on the banner, and you can change your mind with "Analytics choices" in the footer. If you decline, no
          analytics script is requested.
        </p>
        <h2>Who sees it</h2>
        <p>
          Contact messages are visible to the operator of this deployment, and may be forwarded to them by email if the
          operator enabled that. We do not sell or share them. Third parties are involved only when you choose them:
          PayPal if you connect a sandbox account, a language-model provider if the operator set an API key, and Google
          if you accept analytics.
        </p>
        <h2>Your choices</h2>
        <p>
          To see, correct or delete a contact message, <a href="/contact">contact us</a> from the address you used. Demo
          records can be removed by deleting the deployment's database.
        </p>
        <h2>Changes</h2>
        <p>Material changes will be reflected on this page.</p>
      </div>
    </Shell>
  );
}

function Contact() {
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const [msg, setMsg] = useState("");
  const cfg = useApi("/api/config");
  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    setState("sending");
    setMsg("");
    try {
      await api.post("/api/contact", Object.fromEntries(f.entries()));
      setState("sent");
      setMsg("Thank you. Your message was received.");
      form.reset();
    } catch (err) {
      setState("error");
      setMsg((err as Error).message);
    }
  }
  return (
    <Shell page="contact" trail={[["Home", "/"], ["Contact", null]]}>
      <section className="hero">
        <h1>Contact</h1>
        <p>Questions, feedback, or a problem with the demo? Send a message. We read everything.</p>
      </section>
      <form className="form" method="post" action="/api/contact" onSubmit={onSubmit} aria-describedby="contact-status">
        <label className="field">
          Your name
          <input type="text" name="name" required maxLength={100} autoComplete="name" />
        </label>
        <label className="field">
          Email address <span className="hint">so we can reply</span>
          <input type="email" name="email" required maxLength={200} autoComplete="email" />
        </label>
        <label className="field">
          Subject <span className="hint">optional</span>
          <input type="text" name="subject" maxLength={150} />
        </label>
        <label className="field">
          Message <span className="hint">10 to 3000 characters</span>
          <textarea name="message" required minLength={10} maxLength={3000} />
        </label>
        <div className="hp" aria-hidden="true">
          <label>Leave this empty <input type="text" name="website" tabIndex={-1} autoComplete="off" /></label>
        </div>
        <div className="row">
          <button className="btn go" type="submit" disabled={state === "sending"}>
            {state === "sending" ? "Sending" : "Send message"}
          </button>
          <span className="small muted">We store your message to reply; see the <a href="/privacy">privacy policy</a>.</span>
        </div>
        <div id="contact-status" className="status" role="status" aria-live="polite">
          {state === "sent" && <span className="seal ok">{msg}</span>}
          {state === "error" && <Err>{msg}</Err>}
        </div>
      </form>
      {cfg.data?.contact_email && (
        <p className="small muted" style={{ marginTop: "var(--s5)" }}>
          Prefer email? <a href={`mailto:${cfg.data.contact_email}`}>{cfg.data.contact_email}</a>
        </p>
      )}
    </Shell>
  );
}

function NotFound() {
  return (
    <Shell page="404">
      <section className="hero">
        <h1>That page does not exist</h1>
        <p>The address may be mistyped or the page may have moved. Here is where to go instead.</p>
      </section>
      <ul className="prose">
        <li><a href="/">The buyer app</a></li>
        <li><a href="/console.html">The merchant console</a></li>
        <li><a href="/about">About Mandate</a></li>
        <li><a href="/contact">Contact us</a></li>
      </ul>
    </Shell>
  );
}

export function SitePage({ page }: { page: string }) {
  if (page === "about") return <About />;
  if (page === "privacy") return <Privacy />;
  if (page === "contact") return <Contact />;
  return <NotFound />;
}
