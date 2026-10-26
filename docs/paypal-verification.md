# PayPal facts: what was checked, against what, and what was not

Written so a reviewer can tell documentation-checked from sandbox-proven. **Nothing here has been run against a
live PayPal sandbox by the author** (no sandbox credentials were available while building); `python -m
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

## Not verified

* End-to-end Orders create, approve, capture and refund against `api-m.sandbox.paypal.com`.
* Real behaviour of `seller_protection_applied`, `evidence_requested` and buyer-offer actions.
* PayPal Chargeback Protection and Seller Protection eligibility: Mandate treats "PayPal's own protection already
  applies" as a reason to defer to PayPal's process (a visible case in the tests and the simulator), and does
  not claim to evaluate eligibility itself.

The simulator (`apps/api/mandate/sim/paypal.py`) is built from the same documents. It shows that Mandate's calls are
internally consistent with them, not that PayPal behaves this way.
