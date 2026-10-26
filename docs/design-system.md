# Design system

Source of truth: `apps/web/src/tokens.css` (tokens) and `ui.css` (components). Nothing in `ui.css` hardcodes a
colour, size or radius that has a token.

## Principles

1. **Instrument, not interface chrome.** One engraved element (the certificate band); everything else is quiet.
2. **Colour means one thing.** Seal blue = authorised/primary. Green = proof verified. Red = refused/destructive.
   Amber = a human is needed. Never used decoratively, never the only signal.
3. **Proof is shown, not claimed.** Hashes, signatures and audit state are visible and verifiable.
4. **Reduced everything.** Motion, transparency and low contrast each have a fallback.

## Tokens

| Group | Tokens |
|---|---|
| Surface | `--canvas`, `--card`, `--sunken`, `--hairline`, `--hairline-strong` |
| Content | `--ink`, `--ink-2` |
| Signal | `--seal`, `--verified`, `--refused`, `--hold`, `--on-seal` and `*-wash` tints |
| Type | `--font-display` (Instrument Serif), `--font-ui` (IBM Plex Sans), `--font-data` (IBM Plex Mono); `--text-xs` 12.5, `-sm` 14, `-md` 16, `-lg` 18, `-xl` 24, `-2xl` clamp(38px, 5.2vw, 60px) |
| Space | `--s1` 4, `--s2` 8, `--s3` 12, `--s4` 16, `--s5` 24, `--s6` 32, `--s7` 48, `--s8` 72 |
| Radius | `--radius-sm` 10, `-md` 14, `-lg` 20, `-pill` |
| Target | `--control` 44 |
| Elevation | `--lift`, `--lift-sm` (hairline ring plus one soft tinted shadow) |
| Motion | `--ease`, `--t-fast` 120 ms, `--t-seal` 650 ms |

## Components

| Component | Variants | States | Notes |
|---|---|---|---|
| `.btn` | default (ink), `go` (seal), `quiet` (outline), `danger` (outline red) | default, hover, active, focus-visible, disabled | 44 px minimum; text says what happens ("Capture payment", not "Submit"). A button keeps its name through the flow |
| Field (`input`, `textarea`) | text, number, multi-line | default, focus (ring plus wash), error text below | label above, 44 px minimum |
| `Seal` | allow, deny, step_up, verified, ok, bad, hold | ring draws once on mount | ring + glyph + label; `aria-hidden` on the SVG, the label is the accessible name |
| `.card` | default | n/a | surface for forms and tables |
| `.instrument` | n/a | active, revoked/expired (seal changes) | the signature element: guilloche band, limits, gauge, chain hash, microprint (decorative, `aria-hidden`) |
| `.slip` | allow, deny, step_up | default; actions appear by order state | one purchase attempt; `aria-live="polite"` list |
| `.chip` | default, strong | n/a | order facts, tabular numerals |
| `.segmented` | n/a | selected, hover, focus | `tablist`: Arrow, Home, End keys, roving tabindex |
| `.kpi` | default, verified, bad | n/a | always visible at the top of the console |
| `.split` + legend | n/a | n/a | fault split user / agent platform / merchant; label states the numbers |
| `.asr` rows | n/a | n/a | bench: bar length is attack success rate, value printed beside it |
| `table` | numeric columns right-aligned | n/a | wraps in `.scroll` below 560 px |
| `.empty`, `.note`, `.info`, `.err` | n/a | n/a | an empty state names the next action; errors say what happened and how to fix it |

## Audit of the implementation

| Check | Result |
|---|---|
| Hardcoded colours in `ui.css` | one: the hover shadow `rgb(0 0 0 / 0.35)` on `.btn`; everything else is a token |
| Hardcoded font sizes | 5 literals outside the scale: brand wordmark 1.65rem, instrument numerals 1.9rem (1.6rem on phones), KPI numerals 2rem, microprint 9.5px (decorative) |
| Touch targets under 44 px | none (buttons, inputs, nav links and segmented tabs all use `--control`) |
| Focus visible | 3 px outline on everything interactive |
| Reduced motion / transparency / more contrast | each handled in `ui.css` / `tokens.css` |
