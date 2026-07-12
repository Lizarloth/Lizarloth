import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, Index, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# ── Where the database file lives ─────────────────────────────────────────
# On Railway, attach a Volume mounted at /data so the DB survives redeploys.
# DATA_DIR can be overridden with an env var; it falls back to the current
# folder locally (so the local runner / your PC keep working unchanged).
# Hard-coded to the Railway volume mounted at /data so the DB survives
# redeploys. Falls back to the local folder only if /data does not exist
# (e.g. when running on your own PC).
if os.path.isdir("/data"):
    DATABASE_URL = "sqlite:////data/pricedge.db"
else:
    DATABASE_URL = "sqlite:///./pricedge.db"
print(f"🗄  Database at: {DATABASE_URL}  (DATA_DIR-free build)", flush=True)

engine = create_engine(
    DATABASE_URL,
    # check_same_thread off for FastAPI's threadpool; timeout makes a busy
    # connection WAIT up to 30s for a lock instead of instantly raising
    # "database is locked" (which is what the dense --plp ingest stream hit).
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Run on every new SQLite connection. WAL lets readers (GET /api/products,
    dashboard) proceed WHILE the ingest is writing, instead of blocking the
    whole file — the core fix for the 'database is locked' 500s under the
    fast catalogue push. busy_timeout is a second safety net; synchronous
    NORMAL is the safe/fast pairing with WAL."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")   # 30s, in ms
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, unique=True, nullable=False)
    site = Column(String, nullable=False)          # kotsovolos | public | plaisio
    category = Column(String, default="general")
    alert_threshold = Column(Float, nullable=True)
    alert_email = Column(String, nullable=True)
    active = Column(Boolean, default=True)
    is_new = Column(Boolean, default=False)         # flagged by fast-run when first discovered
    first_seen = Column(DateTime, nullable=True)    # when this product first appeared
    retailer_sku = Column(String, nullable=True, index=True)  # site's own SKU id (stable identity for matching)
    ean = Column(String, nullable=True, index=True)           # EAN/GTIN barcode — strongest cross-retailer identity
    mpn = Column(String, nullable=True, index=True)           # manufacturer part number
    created_at = Column(DateTime, default=datetime.utcnow)


class PriceHistory(Base):
    __tablename__ = "price_history"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, nullable=False, index=True)
    price = Column(Float, nullable=True)
    old_price = Column(Float, nullable=True)
    availability = Column(String, nullable=True)
    raw_data = Column(Text, nullable=True)         # full JSON snapshot
    scraped_at = Column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (
        Index("ix_ph_pid_scraped", "product_id", "scraped_at"),
    )


class ProductMatch(Base):
    """Server-computed match groups (one row per product per group). Rebuilt
    after ingest sweeps; the review UI reads groups via /api/matches."""
    __tablename__ = "product_matches"
    id = Column(Integer, primary_key=True, index=True)
    group_key = Column(String, index=True, nullable=False)
    product_id = Column(Integer, index=True, nullable=False)
    method = Column(String, nullable=False)    # ean | mpn | code | fuzzy | override
    status = Column(String, index=True, nullable=False)  # confirmed | codeonly | conflict
    human = Column(Boolean, default=False)
    computed_at = Column(DateTime, default=datetime.utcnow)


class MatchSuggestion(Base):
    """LLM adjudication verdicts for match groups — advisory only. Keyed by the
    group's sorted product-id signature. Never affects matching until a human
    confirms it in the review UI (which then writes a MatchOverride)."""
    __tablename__ = "match_suggestions"
    id = Column(Integer, primary_key=True, index=True)
    sig = Column(String, unique=True, index=True, nullable=False)
    verdict = Column(String, nullable=False)     # same | different | uncertain
    reason = Column(String, nullable=True)
    model = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MatchOverride(Base):
    """Human review verdicts on product pairs. Always win over recomputation:
    'same' force-merges the pair, 'different' force-splits it."""
    __tablename__ = "match_overrides"
    id = Column(Integer, primary_key=True, index=True)
    product_a = Column(Integer, index=True, nullable=False)   # canonical: a < b
    product_b = Column(Integer, index=True, nullable=False)
    verdict = Column(String, nullable=False)                  # same | different
    created_at = Column(DateTime, default=datetime.utcnow)


class Srp(Base):
    """Brand pricing policy: suggested retail price (SRP/RRP) and optional
    minimum advertised price (MAP) per model. model_code is stored normalized
    (uppercase A–Z0–9 only) so it matches the dashboard's extracted codes."""
    __tablename__ = "srp"
    id = Column(Integer, primary_key=True, index=True)
    model_code = Column(String, unique=True, index=True, nullable=False)
    brand = Column(String, nullable=True)
    srp = Column(Float, nullable=False)
    map_price = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, nullable=False)
    product_name = Column(String)
    site = Column(String)
    condition = Column(String)                     # e.g. "price_below"
    threshold = Column(Float)
    current_price = Column(Float)
    triggered_at = Column(DateTime, default=datetime.utcnow)
    email_sent = Column(Boolean, default=False)


def init_db():
    Base.metadata.create_all(bind=engine)
    # lightweight migration: add columns that may not exist on older DBs
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(products)"))}
            if "is_new" not in cols:
                conn.execute(text("ALTER TABLE products ADD COLUMN is_new BOOLEAN DEFAULT 0"))
            if "first_seen" not in cols:
                conn.execute(text("ALTER TABLE products ADD COLUMN first_seen DATETIME"))
            if "retailer_sku" not in cols:
                conn.execute(text("ALTER TABLE products ADD COLUMN retailer_sku VARCHAR"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_retailer_sku ON products (retailer_sku)"))
            if "ean" not in cols:
                conn.execute(text("ALTER TABLE products ADD COLUMN ean VARCHAR"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_ean ON products (ean)"))
            if "mpn" not in cols:
                conn.execute(text("ALTER TABLE products ADD COLUMN mpn VARCHAR"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_mpn ON products (mpn)"))
            # speed up latest-price lookups on existing databases
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ph_pid_scraped ON price_history (product_id, scraped_at)"))
            conn.commit()
        except Exception as e:
            print(f"(migration note: {e})")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()