# Handoff spec: Buyer app and Merchant console

Stack: React 18 + Vite, plain CSS with custom properties (no Tailwind, no UI kit). Two entry pages from one
project: `index.html` (buyer app), `console.html` (merchant console). Tokens: `tokens.css`; see
`docs/design-system.md` for the component catalogue.

## Layout

| | Value |
|---|---|
| Max content width | 1120 px, centred; side padding `--s5` (24), `--s4` (16) under 560 px |
| Buyer grid | two columns `5fr / 6fr`, gap `--s6`; one column under 900 px |
| Console | KPI row (auto-fit, min 170 px), segmented control, panel |
| Top bar | sticky, glass (`backdrop-filter: blur(18px) saturate(1.4)`), 1 px hairline below; solid under `prefers-reduced-transparency` |
| Text measure | hero paragraph 52 ch, body paragraphs up to 68 ch, footer 78 ch |

## Breakpoints

| Width | Changes |
|---|---|
| over 900 | two-column buyer grid |
| 900 and under | single column; slips follow the builder |
| 560 and under | gutters 16 px, nav left-aligned under the brand, numerals 1.6rem, bench rows narrower; no horizontal page scroll (checked at 390 px) |

## Type

| Role | Family | Size | Weight | Tracking |
|---|---|---|---|---|
| Page title | Instrument Serif | clamp(2.4rem, 5.2vw, 3.75rem) | 400 | -0.02em, line-height 1.02, balanced wrap |
| Wordmark | Instrument Serif | 1.65rem | 400 | -0.01em |
| Section title | IBM Plex Sans | 1.125rem | 600 | -0.005em |
| Body | IBM Plex Sans | 1rem / 1.55 | 400 | 0 |
| Secondary | IBM Plex Sans | 0.875rem | 400/500 | 0 |
| Numerals (limits, KPIs) | IBM Plex Sans, tabular lining | 1.9rem / 2rem | 500 | -0.025em |
| Hashes | IBM Plex Mono | 0.78rem | 400 | 0 |

Fonts are bundled through `@fontsource` (no network at runtime).

## Screens and states

### Buyer app

| Region | States |
|---|---|
| Builder card | empty prompt (button disabled), compiling (button disabled), questions (note with one field each, "Update limits"), ready (limits, items, "Sign this mandate"), error (red text under the button) |
| Mandate instrument | active (verified seal), revoked or expired (refused seal, "Revoke" disabled); gauge shows spent (solid) and held (hatched) |
| Search panel | disabled buttons while a request runs or the mandate is not active |
| Decision slip | seal: Allowed, Refused, Needs you. Allowed slips then show order actions by status: CREATED then "Approve in simulated PayPal" (replay) or "Approve in PayPal" link (sandbox), APPROVED then "Capture payment", CAPTURED then "Mark delivered", "Signed receipt", "Open a dispute" (replay only) |
| Receipt | verified or invalid seal, amount, mandate id, PDF link |
| Empty slips | "Nothing to review yet." plus next action |

### Merchant console

| Tab | Contents and states |
|---|---|
| Disputes | per dispute: reason, id, amount, stage, status chip, fault split bar, agent decision with rule and explanation, evidence completeness and hash, buttons (show note, buyer sends an injection, adjudicate). Empty state explains how to create one |
| Needs a human | pending approvals with reasons and recommended action; decided list as a table |
| Analytics | three tallies: guard verdicts, untrusted-input flags, dispute rules |
| Audit log | latest 60 entries; "Verify the whole chain" shows a verified or broken seal |
| Mandate-Bench | bar per configuration (attack success rate), table by family with dev and hold-out rows, false positives. Missing results show an empty state naming `make bench` |

## Interaction and motion

| Element | Trigger | Motion | Duration | Easing |
|---|---|---|---|---|
| Seal ring | slip appears | ring stroke draws once, glyph settles | 650 ms / 300 ms (260 ms delay) | `cubic-bezier(.2,.7,.2,1)` |
| Button | press | translateY(1px) scale(.99) | 120 ms | same |
| Nav, segmented | hover | background and colour | 120 ms | linear |

No other motion. All of it is disabled under `prefers-reduced-motion: reduce`.

## Accessibility

- Focus order follows reading order; every control shows a 3 px focus outline with 3 px offset.
- Landmarks: one `main`, one sticky `header`, `nav` labelled "Main"; regions use `aria-labelledby`.
- Segmented control: `role=tablist`; tabs `aria-selected`, `aria-controls`, roving `tabindex`; Arrow, Home, End.
- Slips list is `aria-live="polite"`; the seal SVG is `aria-hidden` and the seal text is the label.
- Gauge, fault split and bench bars are `role=img` with a full-sentence `aria-label` containing the numbers.
- Colour is never the only signal (seal has glyph and word). Contrast: see `docs/design-critique.md`.
- Decorative microprint and guilloche are `aria-hidden`.

## Edge cases

| Case | Behaviour |
|---|---|
| Long hashes | `short()` keeps 8 leading and 4 trailing characters; full hash only in the signed credential and the receipt |
| Many slips | newest first; page scrolls, no truncation |
| Evidence note | up to 2,000 characters, shown in a wrapped note |
| API failure | red message under the control that failed, with the server's own detail text |
| Replay mode | header chip says so; PayPal approval is simulated; sandbox mode links to PayPal instead |
| Dark appearance | automatic via `prefers-color-scheme`, no in-app toggle |

## Not done

PayPal JS SDK v6 button, i18n, a toggle for appearance, and screen-reader testing on real devices.
