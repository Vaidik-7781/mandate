# Liability Matrix v0.1 (proposal)

A deterministic rule set that says who is at fault when a disputed purchase was made by an AI agent. It is a
proposal for discussion, not legal advice, and no payment network or PayPal policy is implied.

Nine true/false facts go in. Rules R1 to R8 are evaluated in order; **the first match wins**. A language model
may explain the result; it cannot change it (the result object is frozen and the explainer gets a copy).

| Fact | Meaning |
|---|---|
| P1 | The credential chain verifies |
| P2 | The final mandates satisfy every signed constraint |
| P3 | The cart matches the descriptive intent (colour, size, and so on) |
| P4 | No untrusted instruction influenced the choice |
| P5 | The amount charged equals the signed cart |
| P6 | Delivery or fulfilment is proven |
| P7 | The mandate was not revoked at capture |
| P8 | The merchant is authentic and allowed |
| P9 | The user confirmed the compiled fields after an ambiguity was flagged |

| Rule | Fires when | User / Agent platform / Merchant |
|---|---|---|
| R1 | not P7 | 0 / 100 / 0 |
| R2 | not (P1 and P2) | 0 / 100 / 0 |
| R3 | not P5 | 0 / 0 / 100 |
| R4 | not P8 | 0 / 70 / 30 |
| R5 | not P4 | 0 / 100 / 0, or 0 / 70 / 30 if the injected text was merchant-controlled |
| R6 | not P3 | 0 / 100 / 0, or 30 / 70 / 0 if P9 (the user confirmed an ambiguous field) |
| R7 | not P6 | 0 / 0 / 100 |
| R8 | otherwise | 100 / 0 / 0, and the merchant should defend the dispute |

Machine-readable vectors: `vectors/liability-matrix-v0.1.json` (448 cases, generated from the reference
implementation and replayed by `tests/test_vectors.py`).

## Dispute policy that consumes it

In order: (1) taint flags from the buyer's message lead to a human; (2) merchant plus platform fault of 50% or
more leads to a refund (an offer at INQUIRY stage, acceptance at claim stage); (3) seller protection already
applied leads to no action; (4) evidence completeness below 0.8 leads to a human; (5) otherwise fight only if
`p * (amount + fee) > cost_to_fight + fee`, where p comes from a stepped completeness table; (6) amounts above
the money gate (default $100) go to a human with a recommended action. `seller_protection_applied` is read from
a field whose behaviour in the real sandbox is unverified.

## Assumptions and sources

* **The numbers are design choices.** No card-network rule, PayPal policy or court decision assigns 70/30 or 30/70.
  They encode one idea: when a party skipped the stronger control, it carries the loss. That idea is the
  "liability shift" principle that payment networks apply to chip and 3-D Secure transactions, applied here to
  agent-specific controls (signed limits, merchant registry, untrusted-input checks).
* **Ordering is a judgment too.** Revocation and credential failures come first because they are the platform's to
  prevent; fulfilment comes last because it is the only fact the merchant alone controls.
* **Sensitivity.** [liability-sensitivity.md](liability-sensitivity.md) shows which numbers can change what the
  dispute agent does (only the user's share) and which only change who bears the loss.
* **Status.** A proposal for discussion with PayPal, issuers and regulators. Not legal advice, not PayPal policy,
  and not a claim about how any real dispute would be decided.
