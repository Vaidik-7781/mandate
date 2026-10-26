"""How to get a real dispute into the PayPal sandbox so the dispute agent can act on it.

Verified against PayPal's Disputes documentation (research notes in docs/paypal-verification.md); NOT yet run
against a live sandbox account by the author, because that needs your sandbox credentials.

Two routes:

1. By hand (reliable). Log in to https://sandbox.paypal.com with the BUYER test account, open the completed
   transaction, choose "Report a problem" and file an item-not-received dispute. PayPal sends
   CUSTOMER.DISPUTE.CREATED to the webhook URL registered for your REST app (PAYPAL_WEBHOOK_ID plus a public
   HTTPS URL pointing at /webhooks/paypal).

2. By API (sandbox only). PayPal documents `POST /v1/customer/disputes`, available only in the sandbox, which
   creates a dispute on behalf of a buyer. It needs a `PayPal-Auth-Assertion` header for the buyer and the
   disputes scopes (including `https://uri.paypal.com/services/disputes/update-buyer`) on the REST app, so it is
   easy to get a 403 the first time. A merchant-side client cannot open a dispute against its own transaction.

Replay mode needs none of this: `POST /api/orders/{id}/sim-dispute` opens one inside the in-process simulator.

    python scripts/create_dispute.py         # prints these instructions
"""

from __future__ import annotations

print(__doc__)
