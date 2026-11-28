# Pricedge: notes for Claude sessions

Competitor price monitoring for a Greek appliance seller (own brands: Whirlpool,
Indesit, Beko). Rivals are tracked at three retailers: Kotsovolos, Plaisio, Public.
The owner is not a developer: explain in plain words, and do the work rather than
handing over commands where possible.

## Layout

- `frontend/` - the dashboard, one file (`index.html`), served by Netlify at
  pricedge.com. Netlify publishes `frontend/` from the default branch
  `claude/price-intelligence-audit-5xh7xs`; merging into it deploys.
  The build label in the page (e.g. `build 2026-11-27-S58`) changes only when
  `index.html` changes. Also here: `robots.txt` (Allow: / on purpose, so Google
  can read the noindex), `_headers` (X-Robots-Tag: noindex, nofollow), `404.html`.
- `backend/` - FastAPI on Railway (pricedge-backend-production.up.railway.app).
  Dashboard reads need `X-API-Key`; scraper writes under `/api/ingest/*` need
  `X-Ingest-Token` (Railway env var `INGEST_TOKEN`). These are two different values.
- `scraper/` - runs on the owner's Windows PC, not in the cloud: the retailers
  block cloud servers (Plaisio product pages return 403 there).
  - `run_daily.cmd` is run by the Windows scheduled task "Pricedge daily scrape".
    It does `git pull --ff-only` first, then
    `local_runner.py --plp --enrich-prices --market --check-stock --reviews --enrich-specs --due`.
    Output goes to `scraper\logs\daily.log`.
  - Needs env vars `INGEST_TOKEN` and `PRICEDGE_API_KEY` (set with `setx`).
  - Run Python as `py -X utf8 ...` (Greek text).
- `AUDIT.md` - earlier audit; notes the backend write endpoints lack proper auth.

## What the dashboard has

Overview tab: Price index (own brands vs same-spec rivals), Shelf share by
retailer and week, Range gaps (spec segments rivals cover and we don't).
Signals tab: Promo frequency (incl. Plaisio, from price history).
Exports: Daily brief Excel (My models, Price changes, Stock issues, New listings,
Rivals on promo), Same-specs Excel (matches by identical specs and colour family),
Competitor monthly prices. Charts animate once when scrolled into view and respect
reduced-motion.

## Where things stand (28 Nov 2026)

- Spec / energy-class backlog: Plaisio fridge listings lack the energy class.
  The backend borrows it from the same model at another shop; the spec pass reads
  product pages to fill the rest. Backlog on 28 Nov: Plaisio 120 with no class,
  271 borrowed; Kotsovolos 91; Public 18. The daily run reads up to 150 pages a
  day, so it should clear in about three days. Check `daily.log` for
  `Spec pass DONE: N pages with a spec table, M with an energy class`.
- Public is paused until 1 Dec 2026 (Akamai bot block since 13 Nov;
  `PUBLIC_PAUSED_UNTIL` in `local_runner.py`). BestPrice covers ~60 Public models a day.
- Waiting on the owner: their minimum-price (SRP/MAP) list, to fill the compliance
  columns already in the Daily brief.
- Cloud sessions cannot reach this PC's folders (Remote Control on this Windows
  install doesn't offer them), so PC-side checks happen in a session started here.

## Working rules

- Branch, then pull request into `claude/price-intelligence-audit-5xh7xs`; the
  owner has okayed merging small, checked changes.
- After a frontend change, confirm on pricedge.com that it deployed.
- Netlify deploy credits are limited: batch changes into few merges. `netlify.toml`
  makes Netlify skip builds when `frontend/` didn't change.
- Never commit tokens or keys.
