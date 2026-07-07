# Pricedge — competitive price intelligence (GR appliances)

Tracks prices, availability and specs across **Kotsovolos, Public and Plaisio**
(cooling + laundry departments) and turns them into positioning views, parity
checks and threat alerts for the Whirlpool / Indesit / Beko brand team.

## Repository layout

| Path | What it is |
|---|---|
| `backend/` | FastAPI + SQLite API deployed on Railway (`main.py`, `database.py`, `exports.py`, `alerts.py` — SMTP threshold alerts) |
| `scraper/` | Local runner (`local_runner.py`) plus the per-retailer catalogue fetchers it imports (`public_plp.py`, `kotsovolos_plp.py`, `plaisio_plp.py`); `plp_scraper.py` inspection build |
| `frontend/` | Single-file dashboard (`index.html`) served statically |
| `requirements.txt` | Python dependencies (backend + runner combined) |
| `AUDIT.md` | Full platform audit (five pillars) + backend addendum |

**Not yet committed** (still only on the workstation): `scrapers.py`
(imported by `backend/main.py` for the cloud-scrape endpoints). Please add it
so the repo is the complete source of truth. Note `requirements.txt` does not
pin `requests` (used by the runner and Kotsovolos/Public fetchers) — it
currently arrives only as a transitive dependency.

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `INGEST_TOKEN` | Railway **and** the runner PC | Auth for `POST /api/ingest/*`. Never hard-code it in source. **Rotate the old token** — it previously lived in `local_runner.py`. |
| `API_KEY` | Railway | When set, every other `/api/*` route requires the `X-API-Key` header (the dashboard prompts for it once and stores it). Unset = open (a startup warning is printed). |
| `PRICEDGE_API` | runner PC | Backend base URL (defaults to the Railway production URL). |
| `SCRAPE_MODE` | Railway | `local` (default): the backend scheduler is a no-op and the PC runner does the scraping. `cloud`: the backend scrapes on its own schedule. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `FROM_EMAIL` | Railway | Threshold-alert email delivery (`backend/alerts.py`). Alerts are recorded either way; emails send once `SMTP_USER`/`SMTP_PASS` are set (for Gmail use an app password). |

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
