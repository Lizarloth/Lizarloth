from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import json
import os

from database import init_db, get_db, Product, PriceHistory, Alert, Srp
# The scraper module stays on the local PC by design — retailer sites flag
# datacenter IPs, so all scraping runs from a residential connection via
# local_runner.py. The backend must still boot without it: fall back to a
# local detect_site and disable the cloud-scrape endpoints.
try:
    from scrapers import scrape_url, scrape_batch, detect_site
    CLOUD_SCRAPE_AVAILABLE = True
except ImportError:
    CLOUD_SCRAPE_AVAILABLE = False

    def detect_site(url):
        u = (url or "").lower()
        if "kotsovolos" in u: return "kotsovolos"
        if "plaisio" in u: return "plaisio"
        if "public.gr" in u: return "public"
        return None

    def scrape_batch(urls):
        print("⚠ scrape_batch called but scrapers.py is not deployed (local-runner architecture)")
        return []

    def scrape_url(url):
        return {}
from alerts import check_and_alert
from exports import (
    export_price_history_csv,
    export_alerts_csv,
    export_competitors_csv,
    export_full_excel,
)

# ── App setup ────────────────────────────────────────────────────────────
app = FastAPI(title="PricEdge API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Dashboard API key ────────────────────────────────────────────────────
# Set API_KEY on Railway to lock every /api/* route (reads, writes, exports)
# behind an X-API-Key header. /api/ingest/* keeps its own X-Ingest-Token
# check. If API_KEY is unset the API stays open (rollout-safe) and a warning
# is printed at startup — set it as soon as the frontend has the key.
DASHBOARD_API_KEY = os.getenv("API_KEY", "")
if not DASHBOARD_API_KEY:
    print("⚠ API_KEY is not set — the dashboard API is open to anyone with the URL. "
          "Set the API_KEY environment variable to enable authentication.", flush=True)


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    if (
        DASHBOARD_API_KEY
        and request.method != "OPTIONS"                      # CORS preflight must pass
        and request.url.path.startswith("/api/")
        and not request.url.path.startswith("/api/ingest/")  # runner uses X-Ingest-Token
    ):
        supplied = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if supplied != DASHBOARD_API_KEY:
            # This short-circuit bypasses CORSMiddleware (which sits INSIDE
            # this middleware in the stack), so the CORS header must be added
            # by hand: without it, browsers hide the 401 from cross-origin
            # JavaScript and the dashboard can't show its API-key prompt —
            # it reads as a network failure and falls back to demo data.
            return JSONResponse({"detail": "Invalid or missing X-API-Key"},
                                status_code=401,
                                headers={"Access-Control-Allow-Origin": "*",
                                         "Access-Control-Expose-Headers": "*"})
    return await call_next(request)


# ── Pydantic schemas ─────────────────────────────────────────────────────
class ProductCreate(BaseModel):
    url: str
    name: Optional[str] = None
    category: Optional[str] = "general"
    alert_threshold: Optional[float] = None
    alert_email: Optional[str] = None


class ScrapeRequest(BaseModel):
    urls: list[str]


class IngestPayload(BaseModel):
    results: list[dict]


class SrpRow(BaseModel):
    model_config = {"protected_namespaces": ()}   # allow the model_code field name
    model_code: str
    srp: float
    map_price: Optional[float] = None
    brand: Optional[str] = None


class SrpImportPayload(BaseModel):
    rows: list[SrpRow]
    replace: bool = False           # true = wipe the table before importing


# ── DB session for background jobs ───────────────────────────────────────
def _fresh_db():
    gen = get_db()
    return gen, next(gen)


def _close_db(gen):
    try:
        next(gen)
    except StopIteration:
        pass


# ── Helpers ──────────────────────────────────────────────────────────────
def save_scrape_result(result: dict, db: Session):
    product = db.query(Product).filter(Product.url == result["url"]).first()
    if not product:
        return

    record = PriceHistory(
        product_id=product.id,
        price=result.get("price"),
        old_price=result.get("old_price"),
        availability=result.get("availability"),
        raw_data=json.dumps(result, ensure_ascii=False),
    )
    db.add(record)

    if result.get("name") and result["name"] != "N/A":
        product.name = result["name"]

    db.commit()

    if result.get("price"):
        check_and_alert(product, result["price"], db, Alert)


def run_scrape_job(urls: list[str]):
    """Runs in the background with its OWN database session."""
    gen, db = _fresh_db()
    try:
        results = scrape_batch(urls)
        saved = 0
        for r in results:
            if r.get("success"):
                save_scrape_result(r, db)
                saved += 1
        print(f"✔ Scrape job done: {saved}/{len(results)} saved")
    finally:
        _close_db(gen)


def _latest_history(db: Session, product_id: int):
    return (
        db.query(PriceHistory)
        .filter(PriceHistory.product_id == product_id)
        .order_by(PriceHistory.scraped_at.desc())
        .first()
    )


# ── Scheduler ────────────────────────────────────────────────────────────
_scheduler = None


def _send_plain_email(subject: str, body: str) -> bool:
    """Minimal plain-text mail via the same SMTP env config alerts.py uses."""
    import smtplib
    from email.mime.text import MIMEText
    from alerts import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_EMAIL
    to = os.getenv("ALERT_EMAIL", SMTP_USER)
    if not SMTP_USER or not SMTP_PASS or not to:
        print("⚠ (alert not emailed — SMTP_USER/SMTP_PASS/ALERT_EMAIL not configured)")
        return False
    msg = MIMEText(body)
    msg["Subject"], msg["From"], msg["To"] = subject, FROM_EMAIL, to
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(FROM_EMAIL, [to], msg.as_string())
        print(f"✉ alert email sent to {to}: {subject}")
        return True
    except Exception as e:
        print(f"❌ alert email failed: {e}")
        return False


_stale_notified: dict = {}   # site -> ISO date the last stale email went out


def _check_staleness():
    """Dead-man's switch for the local-runner architecture: all scraping runs
    on a residential PC (datacenter IPs get flagged), so if that PC misses its
    runs, nothing else would tell anyone. On each scheduler tick, compare each
    site's newest scrape against STALE_ALERT_HOURS and email at most once per
    site per day when data has gone stale."""
    hours = float(os.getenv("STALE_ALERT_HOURS", "36"))
    gen, db = _fresh_db()
    try:
        rows = (db.query(Product.site, func.max(PriceHistory.scraped_at))
                .join(PriceHistory, PriceHistory.product_id == Product.id)
                .filter(Product.active == True)
                .group_by(Product.site).all())
    finally:
        _close_db(gen)
    if not rows:
        return
    now = datetime.utcnow()
    stale = [(s, mx) for s, mx in rows
             if mx and (now - mx).total_seconds() > hours * 3600]
    if not stale:
        print("⏰ freshness check: all retailers scraped within "
              f"{hours:.0f}h — local runner healthy")
        return
    detail = "; ".join(f"{s}: last scrape {mx:%Y-%m-%d %H:%M} UTC" for s, mx in stale)
    print(f"⚠ STALE DATA — {detail}")
    today = now.date().isoformat()
    to_notify = [s for s, _ in stale if _stale_notified.get(s) != today]
    if not to_notify:
        return
    if _send_plain_email(
        subject=f"Pricedge: data stale for {', '.join(to_notify)}",
        body=("The local runner appears to have missed its scheduled runs.\n\n"
              f"{detail}\n\n"
              "Check the PC and run:  py local_runner.py --plp --due --max-minutes 40"),
    ):
        for s in to_notify:
            _stale_notified[s] = today


def scheduled_scrape():
    mode = os.getenv("SCRAPE_MODE", "local").lower()
    if mode != "cloud" or not CLOUD_SCRAPE_AVAILABLE:
        if mode == "cloud" and not CLOUD_SCRAPE_AVAILABLE:
            print("⏰ SCRAPE_MODE=cloud but scrapers.py is not deployed — "
                  "falling back to freshness watchdog only.")
        _check_staleness()
        return
    gen, db = _fresh_db()
    try:
        urls = [p.url for p in db.query(Product).filter(Product.active == True).all()]
    finally:
        _close_db(gen)
    if urls:
        print(f"⏰ Scheduled price sweep: {len(urls)} products")
        run_scrape_job(urls)
    else:
        print("⏰ Scheduled sweep skipped — no active products")


@app.on_event("startup")
def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        tz = None
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Athens")
        except Exception:
            pass
        _scheduler = BackgroundScheduler(timezone=tz) if tz else BackgroundScheduler()
        _scheduler.add_job(
            scheduled_scrape,
            CronTrigger(hour="9,17", minute=0),
            id="twice_daily_sweep",
            replace_existing=True,
        )
        _scheduler.start()
        print("⏰ Scheduler running — price sweeps daily at 09:00 & 17:00 (Athens)")
    except Exception as e:
        print(f"⚠ Scheduler not started: {e} — add 'apscheduler' to requirements.txt")


# ── Routes ───────────────────────────────────────────────────────────────
@app.get("/")
def root():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    return {"message": "PricEdge API running", "docs": "/docs"}


# ── Products ─────────────────────────────────────────────────────────────
@app.get("/api/products")
def list_products(db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.active == True).all()
    # Latest history row per product in ONE query (instead of one query per
    # product): find max(scraped_at) per product_id, then join back for rows.
    latest_at = (
        db.query(PriceHistory.product_id, func.max(PriceHistory.scraped_at).label("mx"))
        .group_by(PriceHistory.product_id)
        .subquery()
    )
    latest_rows = (
        db.query(PriceHistory)
        .join(latest_at, (PriceHistory.product_id == latest_at.c.product_id)
                         & (PriceHistory.scraped_at == latest_at.c.mx))
        .all()
    )
    latest_by_pid = {}
    for h in latest_rows:                      # ties on scraped_at: keep first
        latest_by_pid.setdefault(h.product_id, h)
    out = []
    for p in products:
        latest = latest_by_pid.get(p.id)
        specs, image = {}, None
        if latest and latest.raw_data:
            try:
                raw = json.loads(latest.raw_data)
                specs = raw.get("specs", {}) or {}
                image = raw.get("image") or None   # scraped but previously never served
            except Exception:
                specs = {}
        out.append({
            "id": p.id, "name": p.name, "url": p.url, "site": p.site,
            "sku": getattr(p, "retailer_sku", None),
            "category": p.category, "alert_threshold": p.alert_threshold,
            "alert_email": p.alert_email, "active": p.active,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "price": latest.price if latest else None,
            "old_price": latest.old_price if latest else None,
            "availability": latest.availability if latest else None,
            "specs": specs,
            "image": image,
            "scraped_at": latest.scraped_at.isoformat() if latest and latest.scraped_at else None,
        })
    return out


@app.post("/api/products")
def add_product(payload: ProductCreate, db: Session = Depends(get_db)):
    site = detect_site(payload.url)
    if not site:
        raise HTTPException(400, "Unsupported site. Supported: kotsovolos.gr, public.gr, plaisio.gr")

    existing = db.query(Product).filter(Product.url == payload.url).first()
    if existing:
        if existing.active:
            raise HTTPException(400, "Product URL already tracked")
        existing.active = True
        db.commit()
        return {"id": existing.id, "name": existing.name, "site": site, "reactivated": True}

    product = Product(
        url=payload.url,
        name=payload.name or payload.url,
        site=site,
        category=payload.category,
        alert_threshold=payload.alert_threshold,
        alert_email=payload.alert_email,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return {"id": product.id, "name": product.name, "site": site}


@app.delete("/api/products/{product_id}")
def remove_product(product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    product.active = False
    db.commit()
    return {"deleted": product_id}


# ── Local-runner ingest (Option B) ───────────────────────────────────────
@app.post("/api/ingest/results")
def ingest_results(payload: IngestPayload, request: Request, db: Session = Depends(get_db)):
    """Receive scrape results pushed from the local runner on your PC.

    Auth: the X-Ingest-Token header must match the INGEST_TOKEN Railway
    variable. Unknown product URLs are auto-registered as new products, so
    the runner can also push newly discovered SKUs from category crawls.
    """
    token = os.getenv("INGEST_TOKEN", "")
    if not token or request.headers.get("X-Ingest-Token") != token:
        raise HTTPException(401, "Invalid or missing X-Ingest-Token")

    saved = created = skipped = 0
    for r in payload.results:
        url = r.get("url")
        if not url or not r.get("success"):
            skipped += 1
            continue
        product = db.query(Product).filter(Product.url == url).first()
        if not product:
            site = detect_site(url) or r.get("site", "unknown")
            product = Product(
                url=url,
                name=r.get("name") or url,
                site=site,
                category=r.get("category", "general"),
                retailer_sku=str(r["sku"]) if r.get("sku") else None,
                is_new=True,
                first_seen=datetime.utcnow(),
            )
            db.add(product)
            db.commit()
            db.refresh(product)
            created += 1
        else:
            if not product.active:
                product.active = True   # relisted after retirement — bring it back
            if r.get("sku") and not product.retailer_sku:
                # backfill the retailer's own SKU id on already-known products —
                # it's the stable per-site identity that model-code matching lacks
                product.retailer_sku = str(r["sku"])
        save_scrape_result(r, db)
        saved += 1

    print(f"📥 Ingest: {saved} saved, {created} new products, {skipped} skipped")
    return {"saved": saved, "new_products": created, "skipped": skipped}


@app.post("/api/ingest/retire")
def ingest_retire(request: Request, db: Session = Depends(get_db)):
    """Deactivate products the retailers have delisted. The runner calls this
    only after a FULL sweep (all sites, all categories), so anything whose
    newest price reading is older than RETIRE_HOURS wasn't found by that sweep
    — it's gone from the shelf. Retired products keep their history and come
    back automatically if a later scrape sees them again (ingest reactivates)."""
    token = os.getenv("INGEST_TOKEN", "")
    if not token or request.headers.get("X-Ingest-Token") != token:
        raise HTTPException(401, "Invalid or missing X-Ingest-Token")
    hours = float(os.getenv("RETIRE_HOURS", "48"))
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    latest = (
        db.query(PriceHistory.product_id, func.max(PriceHistory.scraped_at).label("mx"))
        .group_by(PriceHistory.product_id)
        .subquery()
    )
    stale_ids = [pid for (pid,) in
                 db.query(latest.c.product_id).filter(latest.c.mx < cutoff).all()]
    retired = 0
    if stale_ids:
        retired = (db.query(Product)
                   .filter(Product.active == True, Product.id.in_(stale_ids))
                   .update({Product.active: False}, synchronize_session=False))
        db.commit()
    print(f"🧹 Retire: {retired} product(s) not seen in {hours:.0f}h deactivated")
    return {"retired": retired, "cutoff_hours": hours}


# ── Cloud scraper endpoints (kept for testing / future proxy mode) ──────
@app.post("/api/scrape")
def scrape_products(payload: ScrapeRequest, background_tasks: BackgroundTasks):
    if not CLOUD_SCRAPE_AVAILABLE:
        raise HTTPException(503, "Cloud scraping is disabled — scraping runs on the local PC (local_runner.py)")
    background_tasks.add_task(run_scrape_job, payload.urls)
    return {"status": "started", "urls": len(payload.urls)}


@app.post("/api/scrape/all")
def scrape_all(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not CLOUD_SCRAPE_AVAILABLE:
        raise HTTPException(503, "Cloud scraping is disabled — scraping runs on the local PC (local_runner.py)")
    products = db.query(Product).filter(Product.active == True).all()
    urls = [p.url for p in products]
    background_tasks.add_task(run_scrape_job, urls)
    return {"status": "started", "products": len(urls)}


# ── SRP / MAP pricing policy ─────────────────────────────────────────────
def _norm_code(c: str) -> str:
    import re
    return re.sub(r"[^A-Z0-9]", "", str(c or "").upper())


@app.get("/api/srp")
def list_srp(db: Session = Depends(get_db)):
    return [
        {
            "model_code": r.model_code, "brand": r.brand,
            "srp": r.srp, "map_price": r.map_price,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in db.query(Srp).all()
    ]


@app.post("/api/srp/import")
def import_srp(payload: SrpImportPayload, db: Session = Depends(get_db)):
    """Upsert SRP/MAP rows (keyed by normalized model code). The dashboard
    parses the brand team's CSV client-side, previews the match against the
    live catalogue, then posts the confirmed rows here."""
    if payload.replace:
        db.query(Srp).delete()
    upserted = skipped = 0
    for row in payload.rows:
        code = _norm_code(row.model_code)
        if not code or row.srp is None or row.srp <= 0:
            skipped += 1
            continue
        rec = db.query(Srp).filter(Srp.model_code == code).first()
        if rec:
            rec.srp = row.srp
            rec.map_price = row.map_price
            rec.brand = row.brand or rec.brand
            rec.updated_at = datetime.utcnow()
        else:
            db.add(Srp(model_code=code, brand=row.brand,
                       srp=row.srp, map_price=row.map_price))
        upserted += 1
    db.commit()
    return {"upserted": upserted, "skipped": skipped, "total": db.query(Srp).count()}


@app.delete("/api/srp/{code}")
def delete_srp(code: str, db: Session = Depends(get_db)):
    rec = db.query(Srp).filter(Srp.model_code == _norm_code(code)).first()
    if not rec:
        raise HTTPException(404, "SRP entry not found")
    db.delete(rec)
    db.commit()
    return {"deleted": rec.model_code}


# ── Data freshness / scrape status ──────────────────────────────────────
@app.get("/api/status")
def data_status(db: Session = Depends(get_db)):
    """Per-retailer freshness: when each site's data was last scraped and how
    many active products it has. The dashboard uses this for its staleness
    badge; a monitoring cron can alert when last_scraped falls behind."""
    rows = (
        db.query(Product.site,
                 func.max(PriceHistory.scraped_at),
                 func.count(func.distinct(Product.id)))
        .join(PriceHistory, PriceHistory.product_id == Product.id)
        .filter(Product.active == True)
        .group_by(Product.site)
        .all()
    )
    return {
        "sites": {
            site: {
                "last_scraped": mx.isoformat() if mx else None,
                "products": n,
            }
            for site, mx, n in rows
        },
        "server_time": datetime.utcnow().isoformat(),
    }


# ── Promo analytics ──────────────────────────────────────────────────────
@app.get("/api/promo-stats")
def promo_stats(days: int = 30, db: Session = Depends(get_db)):
    """Promo activity over a window, computed from price history old_price
    readings. Returns per-retailer aggregates plus one record per product
    that had any promo activity (promo days, spell count, longest spell,
    depths). Brand-level aggregation happens in the dashboard, which owns
    the brand-extraction logic and the active filters."""
    days = 90 if days >= 60 else 30
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(PriceHistory.product_id, PriceHistory.price,
                 PriceHistory.old_price, PriceHistory.scraped_at)
        .join(Product, Product.id == PriceHistory.product_id)
        .filter(Product.active == True, PriceHistory.scraped_at >= cutoff)
        .all()
    )
    # collapse to one observation per product per day: on-promo + deepest cut
    from collections import defaultdict
    daily = defaultdict(dict)          # pid -> {date: depth% (0 = not on promo)}
    for pid, price, old, ts in rows:
        if ts is None:
            continue
        d = ts.date()
        depth = 0.0
        if price and old and old > price + 0.5:
            depth = (1 - price / old) * 100
        if depth > daily[pid].get(d, -1):
            daily[pid][d] = depth
    prod_site = dict(db.query(Product.id, Product.site)
                     .filter(Product.active == True).all())
    site_tot = defaultdict(lambda: {"skus": 0, "promoted": 0, "depths": []})
    products_out = []
    for pid, by_day in daily.items():
        ds = sorted(by_day)
        promo_days = [d for d in ds if by_day[d] > 0]
        depths = [by_day[d] for d in promo_days]
        st = site_tot[prod_site.get(pid, "?")]
        st["skus"] += 1
        if not promo_days:
            continue
        st["promoted"] += 1
        st["depths"].extend(depths)
        # promo spells: runs of promo days, tolerating <=3-day scrape gaps
        spells, max_spell = 1, 0
        start = prev = promo_days[0]
        for d in promo_days[1:]:
            if (d - prev).days <= 3:
                prev = d
            else:
                max_spell = max(max_spell, (prev - start).days + 1)
                spells += 1
                start = prev = d
        max_spell = max(max_spell, (prev - start).days + 1)
        products_out.append({
            "id": pid,
            "days_observed": len(ds),
            "promo_days": len(promo_days),
            "spells": spells,
            "max_spell_days": max_spell,
            "avg_depth": round(sum(depths) / len(depths), 1),
            "max_depth": round(max(depths), 1),
            "current": bool(ds and by_day[ds[-1]] > 0),
        })
    sites = {
        s: {
            "skus_seen": v["skus"],
            "skus_promoted": v["promoted"],
            "promo_share": round(v["promoted"] / v["skus"] * 100, 1) if v["skus"] else 0,
            "avg_depth": round(sum(v["depths"]) / len(v["depths"]), 1) if v["depths"] else 0,
        }
        for s, v in site_tot.items()
    }
    return {"days": days, "sites": sites, "products": products_out}


# ── Price history ────────────────────────────────────────────────────────
@app.get("/api/history")
def get_history(product_id: Optional[int] = None, limit: int = 200,
                days: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(PriceHistory, Product).join(Product, PriceHistory.product_id == Product.id)
    if product_id:
        q = q.filter(PriceHistory.product_id == product_id)
    if days:
        q = q.filter(PriceHistory.scraped_at >= datetime.utcnow() - timedelta(days=days))
    rows = q.order_by(PriceHistory.scraped_at.desc()).limit(limit).all()
    return [
        {
            "id": h.id, "product_id": h.product_id,
            "product": p.name, "site": p.site,
            "price": h.price, "old_price": h.old_price,
            "availability": h.availability,
            "scraped_at": h.scraped_at.isoformat() if h.scraped_at else None,
        }
        for h, p in rows
    ]


# ── Alerts ───────────────────────────────────────────────────────────────
@app.get("/api/alerts")
def get_alerts(limit: int = 50, db: Session = Depends(get_db)):
    alerts = db.query(Alert).order_by(Alert.triggered_at.desc()).limit(limit).all()
    return [
        {
            "id": a.id, "product_name": a.product_name, "site": a.site,
            "condition": a.condition, "threshold": a.threshold,
            "current_price": a.current_price, "email_sent": a.email_sent,
            "triggered_at": a.triggered_at.isoformat() if a.triggered_at else None,
        }
        for a in alerts
    ]


# ── Competitor comparison ────────────────────────────────────────────────
@app.get("/api/competitors")
def competitor_comparison(db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.active == True).all()
    result = []
    for p in products:
        latest = _latest_history(db, p.id)
        result.append({
            "product_id": p.id,
            "name": p.name,
            "site": p.site,
            "url": p.url,
            "latest_price": latest.price if latest else None,
            "scraped_at": latest.scraped_at.isoformat() if latest and latest.scraped_at else None,
        })
    return result


# ── Dashboard summary ────────────────────────────────────────────────────
@app.get("/api/dashboard")
def dashboard_summary(db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.active == True).all()
    alert_count = db.query(Alert).count()
    sites = list(set(p.site for p in products))
    return {
        "products_tracked": len(products),
        "sites_monitored": len(sites),
        "active_alerts": alert_count,
        "sites": sites,
        "schedule": "09:00 & 17:00 Europe/Athens (local runner)",
    }


# ── Exports ──────────────────────────────────────────────────────────────
def _csv_history_response(db: Session):
    products = db.query(Product).all()
    history = db.query(PriceHistory).order_by(PriceHistory.scraped_at.desc()).all()
    data = export_price_history_csv(products, history)
    return StreamingResponse(
        iter([data]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=price_history.csv"},
    )


def _xlsx_response(db: Session):
    products = db.query(Product).all()
    history = db.query(PriceHistory).order_by(PriceHistory.scraped_at.desc()).all()
    alerts = db.query(Alert).order_by(Alert.triggered_at.desc()).all()
    data = export_full_excel(products, history, alerts)
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=pricedge_report.xlsx"},
    )


@app.get("/api/export/csv/history")
def export_csv_history(db: Session = Depends(get_db)):
    return _csv_history_response(db)


@app.get("/api/export/csv")
def export_csv_alias(db: Session = Depends(get_db)):
    return _csv_history_response(db)


@app.get("/api/export/excel")
def export_excel_alias(db: Session = Depends(get_db)):
    return _xlsx_response(db)


@app.get("/api/export/xlsx")
def export_xlsx(db: Session = Depends(get_db)):
    return _xlsx_response(db)


@app.get("/api/export/csv/alerts")
def export_csv_alerts(db: Session = Depends(get_db)):
    alerts = db.query(Alert).order_by(Alert.triggered_at.desc()).all()
    data = export_alerts_csv(alerts)
    return StreamingResponse(
        iter([data]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=alerts.csv"},
    )


# ── Run ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    # Railway injects PORT; locally it falls back to 8000. Auto-reload only
    # when developing locally (never under a PORT-managed deployment).
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port,
                reload="PORT" not in os.environ)
