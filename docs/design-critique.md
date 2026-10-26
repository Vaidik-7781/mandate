# Design critique (v1 to v2)

Reviewed with the Apple HIG references (`branding.md`, `motion.md`, `accessibility.md`, `typography.md`,
`layout.md`) and the design-critique framework. The app is a web app, so Apple's *principles and foundations*
apply; its platform conventions (tab bars, menu bar) do not. Where a point has no HIG source it is marked
*judgment*.

## Subject, thesis, signature

Mandate's job is to make a delegated purchase *provable*. The design's thesis: **a mandate looks like a
security-printed instrument** (the engraved guilloche rosette on banknotes and certificates), and every
decision is a quiet, legible slip. The signature element is the certificate band on the mandate card. Everything
else is deliberately plain.

## v1 findings and what changed

| # | Finding | Severity | Source | Change |
|---|---|---|---|---|
| 1 | Buttons 38 px tall | High | `accessibility.md › Controls`: default target size 44 pt on touch | Every control is at least 44 px (`--control`) |
| 2 | Large rotated "stamp" labels read as playful, and a stamp is a rotated image-like element with only colour to separate states | High | `branding.md`: "Ensure branding always defers to content"; `motion.md`: "Add motion purposefully" | Seal = ring + glyph + word. Colour is never the only signal. The ring draws once (650 ms); disabled by `prefers-reduced-motion` |
| 3 | Brand blue used on many controls | Medium | `branding.md`: "Apply your app's accent color judiciously ... Minimize its use on controls" | Accent (`--seal`) only for the primary action, the allowed seal and the budget gauge. Neutral buttons are ink or outline |
| 4 | Destructive "Revoke" was a filled red button | Medium | judgment, consistent with `buttons.md` role guidance | Outline danger style, so it is findable but not the loudest thing on the page |
| 5 | Display face used for UI headings and numbers | Medium | `typography.md`: "use a custom font for headlines ... system fonts for body" (via `branding.md`) | Serif (Instrument Serif) only for the page title and wordmark. UI text and numerals are IBM Plex Sans. The serif "1" read as a lowercase l, so numerals moved to Plex |
| 6 | Small mono hash text at 11.8 px | Low | `typography.md › default and minimum sizes` | 12.5 px floor (`--text-xs`), used for hashes and fine print only |
| 7 | Tabs were pills with no keyboard model | Medium | `accessibility.md`: keyboard use must work | Segmented control with `role=tablist`, roving `tabindex`, Arrow/Home/End keys; covered by the e2e test |
| 8 | Informational text ("Dispute opened") shown in error red | Medium | `feedback.md`: feedback in the interface, not alarming | `.info` style; `.err` reserved for failures |
| 9 | No dark appearance review | Medium | `color.md`/`accessibility.md`: check contrast in light and dark | Both palettes defined; all text tokens verified at 4.5:1 or better |
| 10 | Sticky bar blur with no fallback | Low | `materials.md` (reduced transparency) | `prefers-reduced-transparency` swaps the blur for a solid canvas |

## Contrast (computed from the token values)

| Token | On card, light | On card, dark |
|---|---|---|
| ink | 18.2 | 15.8 |
| ink-2 | 7.9 | 7.7 |
| seal | 8.3 | 8.4 |
| verified | 5.8 | 9.5 |
| refused | 6.5 | 8.0 |
| hold | 6.5 | 10.4 |
| on-seal text on seal button | 8.3 | 9.1 |

All meet 4.5:1 (`accessibility.md`: text up to 17 pt). `prefers-contrast: more` raises secondary text to ink and
hairlines to the strong value.

## What works (keep)

- One decision, one slip, with the reason in plain language and the agent's own rationale beneath it.
- The empty states say what to do next.
- Proof is visible where it matters: chain hash on the mandate, signed receipt with a verified seal, audit chain
  status in the console header row.

## Remaining limits

- Not reviewed with a screen reader or on real devices; the audit is from token maths, the DOM roles and
  Chromium screenshots at 1280 and 390 px, light and dark.
- Sentence case is used everywhere (Apple's platforms use title case for buttons; *judgment*: sentence case suits a
  web product and is applied consistently).
- Text does not use Dynamic Type; it uses `rem` units so browser text-size settings scale it.
- Figma-based skills (design-to-code) and Superdesign were not used: no Figma file or Superdesign canvas exists
  for this project. The guidance from those skills was applied by hand.
