# Verifiable Intent on PayPal: proposed binding (draft)

This is Mandate's proposal for how a Verifiable Intent (VI) credential chain could ride on PayPal's existing
APIs. It is **not an official PayPal or VI specification.** VI is pinned at commit
`356c29635f1c44df7de02edb58699ca9f29bece6` (draft v0.1).

## Chain

| Layer | Issuer | Content |
|---|---|---|
| L1 | provider (a demo key stands in for PayPal) | the user's credential |
| L2 | the user, autonomous KB-SD-JWT | open mandates: allowed merchants, allowed items, per-purchase amount range, quantity; the agent key in `cnf.jwk` |
| L3a / L3b | the agent | final payment and checkout mandates, 5 minutes of life, bound to one merchant-signed cart |

`L3a.transaction_id` equals the L3b checkout hash, which is the SHA-256 (base64url) of the merchant-signed
checkout JWT. Budget, occurrence and recurrence limits are not enforceable inside a stateless credential, so
Mandate's ledger enforces them.

## Mapping to PayPal

| VI element | PayPal field | Notes |
|---|---|---|
| order chain digest `b64url(sha256(mandate_digest "." L3a "." L3b))`, 43 chars | `purchase_units[0].custom_id` | ties the PayPal order to the exact credentials |
| one purchase attempt | `PayPal-Request-Id` | idempotent create and capture |
| signed cart total | `amount.value` | integer minor units internally, decimal string only at this boundary |
| capture | `POST /v2/checkout/orders/{id}/capture` | the guard issues a token bound to (order, amount, currency, merchant); the gateway refuses anything else |
| refunds | `POST /v2/payments/captures/{id}/refund` | booked once per (order, tag) whether the API response or the webhook arrives first |
| disputes | Disputes v1 (`provide-evidence`, `accept-claim`, `make-offer`) plus sandbox-only `adjudicate` | |
| events | webhook signature verification: `transmissionId|time|webhookId|crc32(raw body)`, SHA256-RSA, cert URL allow-listed, dedupe by event id | |

## Verification order (Guard)

Mandate status; credential chain and every signed constraint (all violations collected); merchant registry;
request claims equal what is signed; cart against descriptive intent; taint and step-up; atomic ledger
reservation (budget, occurrences, replay set, velocity); gateway token. The VI library's default 300 s clock
skew is reduced to 30 s and L3 expiry is re-checked independently, because the default would double a 5-minute
mandate's lifetime.

## What is verified, and what is not

**Checked against PayPal's published documentation** (see `docs/paypal-verification.md`): `custom_id` accepts up to
255 characters (ours is 43, base64url SHA-256); the documented `evidence_type` values and which ones each dispute
reason accepts (Mandate sends only documented ones); the shape of the multipart `provide-evidence` call; the
sandbox-only `POST /v1/customer/disputes` route for creating a test dispute; and the `allowed_response_options`
field that tells a merchant which accept-claim and offer types a dispute allows.

**Not yet run against the real sandbox** (the author had no sandbox credentials): all of the above end to end,
`evidence_requested` handling, buyer-offer actions, and the `seller_protection_applied` field. The in-process
simulator implements what the documentation says; it is not proof of what PayPal does. Run
`python -m mandate.sandbox_check` with your sandbox keys to turn the first group into evidence
(`sandbox_report.json`).
