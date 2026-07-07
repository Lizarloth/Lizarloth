from fastapi import FastAPI, Depends, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import json
import os

from database import init_db, get_db, Product, PriceHistory, Alert
from scrapers import scrape_url, scrape_batch, detect_site
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


def scheduled_scrape():
    mode = os.getenv("SCRAPE_MODE", "local").lower()
    if mode != "cloud":
        print("⏰ Sweep time — SCRAPE_MODE=local, cloud scraping skipped. "
              "The local runner on your PC handles this sweep.")
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
    out = []
    for p in products:
        latest = _latest_history(db, p.id)
        specs = {}
        if latest and latest.raw_data:
            try:
                specs = json.loads(latest.raw_data).get("specs", {}) or {}
            except Exception:
                specs = {}
        out.append({
            "id": p.id, "name": p.name, "url": p.url, "site": p.site,
            "category": p.category, "alert_threshold": p.alert_threshold,
            "alert_email": p.alert_email, "active": p.active,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "price": latest.price if latest else None,
            "old_price": latest.old_price if latest else None,
            "availability": latest.availability if latest else None,
            "specs": specs,
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
            )
            db.add(product)
            db.commit()
            db.refresh(product)
            created += 1
        save_scrape_result(r, db)
        saved += 1

    print(f"📥 Ingest: {saved} saved, {created} new products, {skipped} skipped")
    return {"saved": saved, "new_products": created, "skipped": skipped}


# ── Cloud scraper endpoints (kept for testing / future proxy mode) ──────
@app.post("/api/scrape")
def scrape_products(payload: ScrapeRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(run_scrape_job, payload.urls)
    return {"status": "started", "urls": len(payload.urls)}


@app.post("/api/scrape/all")
def scrape_all(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.active == True).all()
    urls = [p.url for p in products]
    background_tasks.add_task(run_scrape_job, urls)
    return {"status": "started", "products": len(urls)}


# ── Price history ────────────────────────────────────────────────────────
@app.get("/api/history")
def get_history(product_id: Optional[int] = None, limit: int = 200, db: Session = Depends(get_db)):
    q = db.query(PriceHistory, Product).join(Product, PriceHistory.product_id == Product.id)
    if product_id:
        q = q.filter(PriceHistory.product_id == product_id)
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
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
