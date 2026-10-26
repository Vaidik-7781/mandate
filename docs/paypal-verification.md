# PayPal facts: what was checked, against what, and what was not

Written so a reviewer can tell documentation-checked from sandbox-proven. **Only the Orders flow has been run against a
live PayPal sandbox by the author** (once, 26 Oct 2026: see the Sandbox run section and `docs/sandbox-report.json`); `python -m
mandate.sandbox_check` is the script that produces that evidence.

| Claim the code relies on | Checked against | Status |
|---|---|---|
| `purchase_units[].custom_id` accepts up to 255 characters; Mandate sends 43 (base64url SHA-256 of the credential chain) | Orders v2 `purchase_unit_request` definition on developer.paypal.com | documented |
| Dispute evidence types and which dispute reasons accept which | Disputes "reasons and evidence" page, `disputes/reasons-evidence` | documented; Mandate sends only documented types (`ALLOWED_EVIDENCE_TYPES`), and the simulator rejects others with 422. An earlier build used an undocumented type for unauthorised disputes; this was found by reading that page and fixed |
| `provide-evidence` is a multipart request with an `input` JSON part and file parts | Customer Disputes v1 schema (`api/customer-disputes/v1/schema.json`) | documented |
| Accepting a claim and making an offer depend on the dispute's `allowed_response_options`; `accept_claim_type` is sent only when the dispute lists it | Customer Disputes v1: accept-claim reference | documented |
| A merchant API client cannot open a dispute against its own transaction; buyers do, or sandbox-only `POST /v1/customer/disputes` with a `PayPal-Auth-Assertion` header and the disputes update-buyer scope | Disputes "test and go live" and set-up pages | documented, not run |
| Webhook event names used (`CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED`, `CUSTOMER.DISPUTE.*`) | Webhooks event names reference | documented |
| Webhook verification (certificate URL, signature over the raw body, PayPal's verify-signature postback as a second path) | Webhooks REST reference | implemented to the documentation, tested only against the simulator's signatures |

## Sandbox run (26 October 2026)

`python -m mandate.sandbox_check` ran end to end against `api-m.sandbox.paypal.com` and every step passed (`docs/sandbox-report.json`): OAuth, mandate signed, order created, the 43-character credential-chain digest stored by PayPal as `custom_id` and read back, buyer approval, capture, `custom_id` echoed on the capture, signed receipt verified, revoke refunded the captured order, ledger exposure back to zero, audit chain intact.

What the run taught us. On the first attempt PayPal's approval page showed a generic error, and the order had been created without `return_url` and `cancel_url`; the code now always sends them (locally they default to `http://localhost:8000`). A later attempt was refused with "This seller doesn't accept payments in your currency", which we suspect was a buyer and seller both in India paying in USD. A different buyer account then completed the payment. Pick a sandbox buyer in a region the seller can receive USD from.

## Not verified

* Webhook delivery and signature verification against PayPal's real events (needs a public URL).
* The Disputes API (create, evidence, accept, offer) against the real sandbox.
* Real behaviour of `seller_protection_applied`, `evidence_requested` and buyer-offer actions.
* PayPal Chargeback Protection and Seller Protection eligibility: Mandate treats "PayPal's own protection already
  applies" as a reason to defer to PayPal's process (a visible case in the tests and the simulator), and does
  not claim to evaluate eligibility itself.

The simulator (`apps/api/mandate/sim/paypal.py`) is built from the same documents. It shows that Mandate's calls are
internally consistent with them, not that PayPal behaves this way.
