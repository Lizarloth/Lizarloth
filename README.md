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

**Kept local by design:** `scrapers.py` stays on the workstation and is
gitignored — retailer sites flag datacenter IPs, so all scraping runs from a
residential connection via `local_runner.py`. The backend boots without it:
the cloud-scrape endpoints return 503 and `detect_site` has a built-in
fallback. Note `requirements.txt` does not pin `requests` (used by the runner
and Kotsovolos/Public fetchers) — it currently arrives only as a transitive
dependency.

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `INGEST_TOKEN` | Railway **and** the runner PC | Auth for `POST /api/ingest/*`. Never hard-code it in source. **Rotate the old token** — it previously lived in `local_runner.py`. |
| `API_KEY` | Railway | When set, every other `/api/*` route requires the `X-API-Key` header (the dashboard prompts for it once and stores it). Unset = open (a startup warning is printed). |
| `PRICEDGE_API` | runner PC | Backend base URL (defaults to the Railway production URL). |
| `SCRAPE_MODE` | Railway | `local` (default): the backend scheduler is a no-op and the PC runner does the scraping. `cloud`: the backend scrapes on its own schedule. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `FROM_EMAIL` | Railway | Threshold-alert email delivery (`backend/alerts.py`). Alerts are recorded either way; emails send once `SMTP_USER`/`SMTP_PASS` are set (for Gmail use an app password). |

## Daily operation

All scraping runs on the local PC (residential IP — cloud IPs get flagged):

```bash
# recommended daily path — fast catalogue APIs:
python local_runner.py --plp --max-minutes 40

# subcategory-aware scheduling (niche segments scraped less often):
python local_runner.py --plp --due
```

Schedule it with Windows Task Scheduler so weekends/holidays aren't missed:

```bat
schtasks /Create /TN "Pricedge daily scrape" /SC DAILY /ST 08:30 ^
  /TR "py C:\pricedge\local_runner.py --plp --due --max-minutes 40"
```

**Dead-man's switch:** because the PC is the single point of failure, the
Railway backend checks data freshness at each scheduler tick (09:00/17:00
Athens). If any retailer's newest scrape is older than `STALE_ALERT_HOURS`
(default 36), it emails `ALERT_EMAIL` (falls back to `SMTP_USER`) — at most
once per site per day. Requires the SMTP env vars.

The dashboard's topbar shows per-retailer data freshness (green ≤24 h,
amber ≤48 h, red older) computed from `scraped_at`; `GET /api/status`
exposes the same per-site freshness for external monitoring.

## SRP / MAP money layer

Import the brand team's price list on the dashboard's **Export** page
(CSV: `model_code, srp, map_price?, brand?` — comma or semicolon separated,
Greek number formats accepted, header row optional). Codes are normalized
to uppercase A–Z0–9 and matched against the codes Pricedge extracts from
product names. Once loaded:

- **My Brands** shows an SRP index under every retailer price (with an
  estimated retailer-margin tooltip) plus SRP-coverage / MAP-breach metrics
- **Alerts** lists MAP breaches (any price below MAP) and deep SRP erosion
  (>10 % under SRP when no MAP is set)
- the **match-matrix CSV export** carries SRP / MAP / best-vs-SRP columns

API: `GET /api/srp` · `POST /api/srp/import` (`{rows:[…], replace?:bool}`) ·
`DELETE /api/srp/{code}` — all behind the `X-API-Key` header.
