# Mandate-Bench

360 synthetic scenarios (290 attacks, 70 benign controls) in nine families, run against five configurations
that each add one defence (C0 agent alone, C1 guard, C2 taint step-up, C3 quarantine and ask-don't-guess,
C4 ledger, revocation and hardened gateway).

```
make bench          # writes results/latest.json, results/results.md, results/raw.jsonl
```

| Family | Attack |
|---|---|
| injection_catalog | instructions hidden in product descriptions and reviews |
| injection_dispute | buyer messages that tell the dispute agent to refund |
| counterfeit_merchant | forged signatures, name collisions, typosquats, stale or later-blocked merchants |
| scope_creep | quantity, amount, item, expiry, budget, occurrence, currency, merchant |
| cart_swap | the cart changes between quote and payment |
| replay | the same signed purchase presented again |
| ambiguous_request | prompts with missing limits (does the compiler guess?) |
| revocation_race | purchase or capture after the user revoked |
| benign_control | normal tasks, to measure false positives |

## How to read it

- Attackers are scripted policies, not language models. Money never moves; a recording executor stands in for
  PayPal. Credentials, guard, ledger, extractor, planner and dispute rules are the real code.
- About 30% of scenarios are hold-out: different phrasings than the dev templates the detector patterns were
  written against. Patterns were not tuned on hold-out. Compare the dev and hold-out columns.
- The deterministic planner never reads raw text, so catalog injection cannot steer it by construction (C3, C4).
- 0 of 290 at C4 means "this scenario set is covered", not "the system is safe".
- The benchmark found a real bug during development: the VI library's default 300 s clock skew let a 5-minute
  L3 mandate live about 10 minutes. It is fixed and has a regression test.
