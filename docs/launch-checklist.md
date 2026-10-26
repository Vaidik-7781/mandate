# Launch checklist: the 20 website items

What is built, where it lives, and what only you can finish. Tests: `tests/test_site.py`, browser check: `make e2e`.

| # | Item | Status | Where |
|---|---|---|---|
| 1 | Server-side rendering | Done | Build prerenders every page (`apps/web/scripts/prerender.mjs`); the browser hydrates it |
| 2 | robots.txt | Done | `/robots.txt`, built from `PUBLIC_BASE_URL` or the request host |
| 3 | sitemap.xml | Done | `/sitemap.xml`, five pages, real last-modified dates |
| 4 | Unique title tags | Done | One per page, tested for uniqueness and length |
| 5 | Meta descriptions | Done | One per page, 70-175 characters, unique |
| 6 | One H1 per page | Done | Tested on all five pages |
| 7 | Canonical tags | Done | Absolute URL from `PUBLIC_BASE_URL` |
| 8 | Open Graph images | Done | `/og.png` 1200x630, plus Twitter card tags |
| 9 | Custom 404 | Done | HTML for browsers, JSON for `/api`, status 404, `noindex` |
| 10 | 301 redirects | Done | Old and alternate paths, trailing slash, optional http to https |
| 11 | Alt text | Done | Every image, tested |
| 12 | WebP images | Done | `/img/*.webp`; the OG card and touch icon stay PNG because social crawlers need PNG |
| 13 | Lazy loading | Done | Images use `loading="lazy"`; the AG Grid bundle loads only when a table tab opens |
| 14 | Organization schema | Done | JSON-LD on `/` and `/about` (founder is Vaidik Gupta with the GitHub link; edit the HTML shells if you want it removed) |
| 15 | Breadcrumbs | Done | Visible trail plus BreadcrumbList JSON-LD on inner pages |
| 16 | Internal links | Done | Header, footer and in-page links between all pages |
| 17 | Google Analytics | Needs your ID | GA4 loads only after consent and only if `GA_MEASUREMENT_ID` is set |
| 18 | Search Console | Needs your account | Set `GOOGLE_SITE_VERIFICATION`; submit `/sitemap.xml` |
| 19 | Privacy policy | Done, review it | `/privacy`. Plain-language text, not legal advice |
| 20 | Working contact form | Done | `/contact`; messages are stored, email is optional (SMTP), honeypot and rate limit |

## Before you go public

1. Set `PUBLIC_BASE_URL=https://your-domain` (canonicals, sitemap and Open Graph all use it).
2. Behind a proxy (Render does this) set `FORCE_HTTPS=true`.
3. Create a GA4 property, put the `G-...` ID in `GA_MEASUREMENT_ID`. Nothing loads until a visitor says yes.
4. In Search Console add the site, choose the HTML-tag method, put the token in `GOOGLE_SITE_VERIFICATION`, redeploy,
   verify, then submit `/sitemap.xml`.
5. Contact form: set `CONTACT_EMAIL` (shown on the page). For email delivery add `CONTACT_TO` and `SMTP_*`. Set
   `ADMIN_TOKEN` to read stored messages with `curl -H "Authorization: Bearer $ADMIN_TOKEN" $URL/api/contact/messages`.
6. Read `/privacy` once and change anything that does not match how you run it.
7. Regenerate images after UI changes: `make web && python scripts/make_assets.py && make web`.

## What I could not check

The pages were checked in Chromium and by tests, not by Google. Rich-result eligibility, indexing, GA data and Search
Console verification need your accounts and a public URL.
