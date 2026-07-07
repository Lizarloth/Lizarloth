# Pricedge — competitive price intelligence (GR appliances)

Tracks prices, availability and specs across **Kotsovolos, Public and Plaisio**
(cooling + laundry departments) and turns them into positioning views, parity
checks and threat alerts for the Whirlpool / Indesit / Beko brand team.

## Repository layout

| Path | What it is |
|---|---|
| `backend/` | FastAPI + SQLite API deployed on Railway (`main.py`, `database.py`, `exports.py`) |
| `scraper/` | Local runner (`local_runner.py`) that crawls/scrapes the retailers and pushes to the backend; `plp_scraper.py` inspection build |
| `frontend/` | Single-file dashboard (`index.html`) served statically |
| `AUDIT.md` | Full platform audit (five pillars) + backend addendum |

**Not yet committed** (still only on the workstation): `alerts.py`, `scrapers.py`,
`public_plp.py`, `kotsovolos_plp.py`, `plaisio_plp.py`, `requirements.txt`.
Please add them so the repo is the complete source of truth.

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `INGEST_TOKEN` | Railway **and** the runner PC | Auth for `POST /api/ingest/*`. Never hard-code it in source. **Rotate the old token** — it previously lived in `local_runner.py`. |
| `API_KEY` | Railway | When set, every other `/api/*` route requires the `X-API-Key` header (the dashboard prompts for it once and stores it). Unset = open (a startup warning is printed). |
| `PRICEDGE_API` | runner PC | Backend base URL (defaults to the Railway production URL). |
| `SCRAPE_MODE` | Railway | `local` (default): the backend scheduler is a no-op and the PC runner does the scraping. `cloud`: the backend scrapes on its own schedule. |

## Daily operation

```bash
# on the runner PC (recommended daily path — fast catalogue APIs):
python local_runner.py --plp --max-minutes 40

# subcategory-aware scheduling (niche segments scraped less often):
python local_runner.py --plp --due
```

The dashboard's topbar shows per-retailer data freshness (green ≤24 h,
amber ≤48 h, red older) computed from `scraped_at`; `GET /api/status`
exposes the same per-site freshness for external monitoring.
