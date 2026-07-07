# Pricedge — Price Intelligence Platform Audit

**Scope:** `index.html` (build 2026-06-22-L17) — the Pricedge dashboard for the Greek major-appliance market (Kotsovolos, Public, Plaisio; cooling + laundry departments; own brands Whirlpool / Indesit / Beko), plus the observable backend contract (`/api/products`, `/api/history`, `/api/alerts`, `/api/export/*` on Railway).
**Benchmark:** 2026 market best practice for competitive price intelligence (Prisync / Price2Spy / Dealavo-class capability, plus current LLM-assisted matching practice).
**Date:** 2026-07-07

---

## Executive summary

Pricedge is genuinely strong for an internal tool: the normalization layer handles real Greek-retail messiness (per-site dimension axis order, Greek/Latin lookalike characters, clearance-item exclusion, colour-family folding), the match-confidence system (confirmed / mostly / conflict / code-only) is something commercial tools charge for, and the positioning views (Segments ladder, My Brands parity, threat-based alerts) ask the right business questions.

The gaps cluster in four places:

1. **Freshness is invisible and collection is manual.** Nothing in the UI says when data was last scraped, and scrapes depend on someone running `local_runner.py`.
2. **Matching runs on name-derived model codes only.** No EAN/MPN anchor, no persisted match table, no way to correct a wrong match.
3. **The money layer is switched off.** `brandSRP = {}` and the margin/VAT constants are unused, so there is no MAP/SRP compliance, no margin view — the platform observes prices but doesn't yet judge them.
4. **Insights don't leave the dashboard.** Alerts are recomputed client-side on each visit, email delivery is stubbed, and exports ignore the filters people actually work in.

There is also one **security issue that outranks everything else**: the backend API appears to be unauthenticated and the frontend is a static page whose only protection is `noindex`. Anyone with the URL can read your competitive dataset and call `POST /api/products` / `DELETE /api/products/:id`.

Priorities at a glance:

| Priority | Item | Pillar |
|---|---|---|
| **P0** | Put auth in front of the API and dashboard | (cross-cutting) |
| **P0** | Scheduled scraping + visible "last updated" per retailer | 1 |
| **P0** | Load SRP/MAP data; activate MAP-compliance + margin views | 3 |
| **P1** | EAN/MPN-anchored matching, server-side match table with manual overrides | 2 |
| **P1** | Server-side alert persistence + email/Slack digest delivery | 3 |
| **P1** | "Export this view" (filter-aware) + laundry + alerts exports | 4 |
| **P2** | Validate prices at ingestion instead of patching client-side | 1 |
| **P2** | Deep-linkable URL state (shareable views) | 4 |
| **P2** | LLM match adjudication for `conflict`/`codeonly` pairs; weekly auto-digest | 5 |

---

## 0. Cross-cutting: access control (fix before anything else)

**The problem.** `const API = "https://pricedge-backend-production.up.railway.app"` (line 984) is called with plain `fetch` — no token, no session. The write paths are wide open from the browser: `POST /api/products` (line 3299), `DELETE /api/products/:id` (line 3306), and `GET /api/export/*` opens the backend directly in a new tab (line 3312). The dashboard itself relies on `<meta name="robots" content="noindex">`, which is a request to crawlers, not access control.

**The impact.** Your competitive positioning data — including which SKUs you've flagged as yours and your alert logic — is readable by anyone who finds the URL, including the retailers you monitor. Worse, an outsider can add junk product URLs or delete your tracked catalogue. For a tool that may later hold SRP/MAP and margin data (see §3), this becomes commercially sensitive leakage.

**The solution.**
1. Add a shared-secret header check on the Railway backend (`Authorization: Bearer <token>`), rejecting all unauthenticated requests — a 10-line middleware.
2. Serve the dashboard behind the same gate (Railway private networking + a tiny auth proxy, or Cloudflare Access / basic auth at minimum).
3. Once SRP data lands (§3), move to per-user logins so access can be revoked.
4. Keep `noindex`, but treat it as cosmetic.

---

## Pillar 1 — Data Accuracy & Collection

### 1.1 No freshness indicator anywhere in the UI

**The problem.** The UI never shows when data was last scraped. The topbar, dashboard hero, and product tables have no "last updated" timestamp. The frontend re-fetches every 2 minutes (`setInterval(loadAll, 120000)`, line 3391), which creates an *illusion* of liveness — but the underlying data only changes when someone manually runs the scraper (the empty states literally say "Run `py local_runner.py --plp`", lines 1450, 1628).

**The impact.** A category manager can quote a "current" competitor price that is actually five days old. Every downstream view — parity flags, threat alerts, segment ladders — silently inherits the staleness. This is the single most common failure mode that erodes team trust in price-intelligence tools.

**The solution.**
1. Add a backend `GET /api/status` returning `{retailer: {last_run, products_scraped, errors}}` (one small table updated at the end of each scrape run).
2. Render it in the topbar: "Kotsovolos 6h ago · Public 6h ago · Plaisio 2d ago", with an amber badge past 24h and red past 48h.
3. Store `scraped_at` per product (the history rows already carry it) and show it in product detail / compare rows.
4. Make the 2-minute refresh conditional: fetch `/api/status` first and only re-fetch products when the data version changed (also fixes the re-render churn in §4.5).

### 1.2 Collection is manual, not scheduled

**The problem.** Scrapes are triggered by hand (`local_runner.py --plp`). There is no scheduler, no retry, no failure notification.

**The impact.** Data freshness depends on someone remembering. Weekend and holiday price moves — precisely when Greek electronics retailers run promos — get captured late or not at all. Deal Radar and price history develop gaps that make trend reads unreliable.

**The solution.**
1. Schedule the runner: a Railway cron job (or GitHub Actions cron hitting a `/api/scrape/run` endpoint) at least daily; 2–4×/day for your own brands' SKUs and their direct threats during promo season (Black Friday, January sales).
2. Stagger retailers and add jitter + per-site rate limits so you remain a polite scraper.
3. On scrape failure (site markup change, block), write the error into `/api/status` and send a notification (see §3.3's delivery channel — reuse it).
4. Keep per-run counts; alert yourself when a run returns <80% of the previous product count (the classic sign a site changed its markup and half the catalogue silently vanished).

### 1.3 Bad prices are patched downstream instead of validated at ingestion

**The problem.** The frontend defends against glitch prices in four separate places: movers reject readings outside 40–250% of a product's median (lines 1729–1733), moves >35% are dropped entirely (line 1742), threats ignore rivals below 35% of your price (line 2488), and history plots drop points below 15% of model median (lines 2412, 2420, 2436). The raw glitch data, however, still lives in the database — and flows untouched into the CSV/Excel exports (`/api/export/*` is raw backend data) and into any future consumer.

**The impact.** Every new view must re-implement the same defensive heuristics or it inherits the glitches. Exports — the thing the team hands to management — contain exactly the errors the dashboard hides. And each heuristic has false positives (see 1.4).

**The solution.**
1. Validate at scrape time: compare each price to the product's last known price; flag deviations >35% for re-scrape confirmation (fetch the PDP once more before accepting).
2. Capture the price *type* explicitly: current price, strikethrough/was price, financing-per-month price (a known source of the "€19.99 fridge" glitch), member/loyalty price. Store them in separate fields rather than parsing whichever number appears first.
3. Quarantine failed readings in a `price_anomalies` table instead of the main history — reviewable, not deleted.
4. Then delete three of the four client-side heuristics and keep one thin guard.

### 1.4 The 35% move cap silently discards real promotions

**The problem.** `computeMovers` drops any price change >35% as a presumed parse error (line 1742) — the comment says "real retail repricing is rarely more than ~35%".

**The impact.** During Black Friday / seasonal clearance, 40–60% cuts are real and are *exactly* the moves your team needs in Deal Radar within hours. Right now the radar goes quiet at the moment it matters most.

**The solution.** Don't drop — demote. Show >35% moves in a "needs verification" section of the Deal Radar drawer with the retailer link for a one-click check. Once ingestion-time re-scrape confirmation exists (1.3), promote confirmed big moves to the normal list automatically.

### 1.5 Unknown availability defaults to "In stock"

**The problem.** `availBucket` returns `"In stock"` for any unrecognized availability string (line 1900).

**The impact.** The availability-mix chart systematically overstates stock. New availability phrasings a retailer introduces (which happens routinely) get counted as in-stock instead of surfacing as a parsing gap.

**The solution.** Return `"Unknown"` for unmatched strings, add it to the chart as a grey band, and log unmatched raw strings so the bucket list grows deliberately.

### 1.6 History is fetched with a hard cap that will silently truncate

**The problem.** `api("/api/history?limit=20000")` (line 1303). Three retailers × ~1,000 SKUs × daily scrapes = ~90k rows/month. Within weeks, the cap will cut off older history — and whichever end the backend truncates, movers, charts and the raw log will quietly lose data.

**The impact.** Price-history charts flatten or develop fake gaps; Deal Radar compares the wrong "previous" price; nobody notices because nothing errors.

**The solution.**
1. Add server-side daily aggregation: one row per product/retailer/day (min price of the day) — that's what every chart actually plots.
2. Query by window (`?days=90`) instead of row count.
3. Keep raw readings server-side for audit, but stop shipping them all to the browser.

### 1.7 Coverage stops at three retailers

**The problem.** The market picture is Kotsovolos + Public + Plaisio. In the Greek appliance market, Skroutz (marketplace + price comparison) is the de-facto price anchor — the code even aligns its size bands "to Skroutz, the GR standard" (line 1907) without tracking it.

**The impact.** You can be "in parity" across the three tracked retailers while a Skroutz marketplace seller undercuts everyone — the price consumers actually see first. Threat alerts miss the most aggressive channel.

**The solution.** Add Skroutz product pages as a fourth source (product-level lowest price + shop count is enough; you don't need every seller). Prioritize your own brands' SKUs and their identified threats to keep crawl volume modest. Review Skroutz's ToS/robots and rate-limit accordingly, as you should for the current three.

---

## Pillar 2 — Product Matching

### 2.1 Matching is anchored on name-parsed model codes, with no EAN/MPN

**The problem.** The entire match pipeline keys on `extractModel()` — a token heuristic over the product *name* (lines 1125–1159: stop-words, Greek-letter folding, digit-cluster detection), grouped by exact code, then fuzzy-merged by string containment (`a.includes(b) && b.length>=6`, lines 1179–1187). No EAN/GTIN or manufacturer part number is scraped or used, even though all three retailers expose them on PDPs (in spec tables and/or JSON-LD structured data).

**The impact.** Two failure modes, both costly:
- **Missed matches** — retailer-specific naming ("/EF" suffixes, capacity in the name, Greek descriptors mid-code) yields different keys, so the same fridge shows as two single-retailer products. Your matched-models KPI undercounts; parity breaks go unseen.
- **False merges** — containment merging can fold a genuinely different variant (one extra character can be a capacity or feature tier) into the same group, producing a "parity break" between two different products. That's the error that gets the tool dismissed after one bad meeting.

The confidence system (lines 1195–1231) mitigates this honestly — but it flags problems rather than fixing them.

**The solution.**
1. Extend the scraper to capture EAN/GTIN and MPN per product: parse JSON-LD `Product.gtin13` / `mpn` first; fall back to spec-table rows (all three sites list "Barcode"/"EAN"/"Κωδικός κατασκευαστή" on most PDPs).
2. Match hierarchy: **EAN equality → MPN equality → normalized model code → fuzzy containment (flagged, never silent)**.
3. Compute matches **server-side once per scrape run** into a `product_matches` table (see also 2.3), with the confidence verdict stored per group.

### 2.2 No way to review or correct a match

**The problem.** The `conflict` confidence state exists ("code matches but specs differ", line 2327), but there is no screen to inspect those pairs, no confirm/reject action, and no persistence — a wrong match can only be fixed by editing `extractModel`'s heuristics.

**The impact.** Known-suspect matches stay wrong forever and feed the parity table, threat alerts, and exports. Commercial tools win here purely on workflow: a human confirms once, and the decision sticks.

**The solution.**
1. Add a **Match review** page listing `conflict` and `codeonly` groups: side-by-side names, specs, images, price, retailer links.
2. Two buttons — *Confirm match* / *Split* — writing to a backend `match_overrides` table (`code_a, code_b, verdict, reviewed_by, reviewed_at`).
3. Apply overrides as the last step of server-side match building; overridden groups show a "human-verified" badge (your `conf` chip already has the visual language for this).
4. Feed it with the LLM adjudicator from §5.3 so a human only reviews the model's uncertain calls.

### 2.3 Matching recomputes O(n²) in the browser on every render

**The problem.** `buildMatches` (containment merge is quadratic over group keys) runs on the client repeatedly — twice inside `renderDashboard` alone (lines 1814, 1817), again in `mineGroup`/`similarGroups`/`availBrands` on the My SKUs page (lines 2907, 2929, 3010), and again on export (line 3315). Every 2-minute refresh re-runs all of it.

**The impact.** At 1,000+ listings per department this becomes seconds of main-thread jank per render on a mid laptop — and results can differ view-to-view as filters change the input set, so "matched models" numbers subtly disagree between screens.

**The solution.** Compute matches server-side per scrape run (one canonical match table), serve them via `/api/matches`, and reduce the client to filtering/annotating. This also makes the match-review workflow (2.2) and exports (4.2) consistent by construction.

### 2.4 `rivalModelKey` unconditionally drops the last character

**The problem.** To merge colour variants, `rivalModelKey` strips a known suffix list and then *always* deletes the final character: `c=c.replace(/.$/,"")` (line 2525).

**The impact.** Distinct models whose codes differ only in the final character — commonly a capacity digit or feature tier, not a finish — collapse into one "rival" in alert grouping. An alert card can show one rival with mixed offers that are actually two different machines at different spec levels, misstating the gap.

**The solution.** Only strip the final character when the remaining stem is identical *and* the specs agree (reuse the group-confidence comparators from `enrichGroup`); otherwise keep codes distinct. Longer term this dissolves into EAN-based identity (2.1).

### 2.5 Laundry type fallback mislabels dryers as washing machines

**The problem.** In `laundryNorm`, a product tagged laundry only via `__dept` defaults to `type = "washing_machine"` (line 1081).

**The impact.** A dryer without a category lands in the washer ladder and — worse — in `computeLaundryThreats`, where type + kg-tier is the *entire* comparability gate (line 2550): a cheap mislabelled dryer can fire a false "disruption" against one of your washers.

**The solution.** Infer type from signals already scraped before defaulting: presence of `Χωρητικότητα Στεγνώματος` **without** `Χωρητικότητα Πλύσης` → dryer; both → washer-dryer; name keywords (στεγνωτήριο / πλυντήριο) as tiebreak; else `unknown` and excluded from threats (not silently binned as a washer).

---

## Pillar 3 — Actionable Insights

### 3.1 The money layer is dormant: no SRP, no MAP, no margin

**The problem.** The scaffolding exists — `BRAND_SETTINGS = { marginPct: 30, vatPct: 24 }` and `let brandSRP = {}` ("supplied later by the user", lines 992–993); the My Brands page even announces "the cost layer activates once you add SRPs" (line 801). But nothing loads SRPs, so nothing computes MAP compliance, price-index vs SRP, or margin impact. Today the platform answers "where do prices sit?" but not "is anyone violating our pricing policy?" or "what is this costing us?"

**The impact.** This is the difference between a market observatory and a price-intelligence tool. Parity breaks are flagged relative to *other retailers*, not to *your policy* — so a retailer selling 15% under SRP across the board looks "in parity" and raises no alarm. Every conversation with a retailer that should start with "you're X% under agreed SRP on N models" currently requires manual spreadsheet work.

**The solution.** (Highest-value item in this audit.)
1. Backend table `srp (model_code | ean, srp, map_price?, valid_from)` + `POST /api/srp/import` accepting the CSV your brand team already maintains.
2. Frontend: an *Import SRPs* card on the Exports page (rename it **Data**) — upload, preview matched/unmatched codes, confirm. Unmatched codes feed the match-review queue (2.2).
3. Once loaded, activate per model: **SRP index** (price ÷ SRP, per retailer), **MAP violation flag** (price < MAP), and a **My Brands** column showing worst index per model.
4. New alert class: MAP/SRP violation — retailer, model, depth, duration (first-seen from history). Duration matters: "Plaisio has been 12% under SRP for 18 days" is an enforcement email that writes itself.
5. Wire the dormant margin/VAT constants into a per-model "estimated retailer margin at current price" column to prioritize which violations to chase first.

### 3.2 Insights describe position but never recommend an action

**The problem.** The insight lines are genuinely good at *describing*: "#4 of 31, €40 above entry, 3 rivals below you". No view proposes what to do about it, and the threat cards don't distinguish a rival's temporary promo from a permanent reprice — even though the scraper already captures `old_price` (used only for strikethrough display in `fmtWas`, line 1384).

**The impact.** Every alert requires an analyst to decide from scratch: is this promo-driven (wait it out / match with promo) or structural (escalate to key-account manager)? That triage is mechanical and should be pre-computed.

**The solution.**
1. Tag each threat with **promo context**: rival has an active was-price → "promotional undercut"; no was-price and stable in history ≥14 days → "structural reprice". One extra field, data already scraped.
2. Add a **recommended action** per alert from a small rule table you control: structural MAP breach → "escalate to KAM"; promo undercut within X% → "monitor, expires with promo"; disruption (better spec, cheaper) → "review SRP positioning of [model]".
3. On Segments, add a **target-position** setting per segment ("we want to hold #1–#3 entry in 8kg washers") and flag segments where you've slipped out of target — turning the ladder from a report into a scoreboard.

### 3.3 Alerts exist only inside the browser session

**The problem.** Threats are recomputed client-side on every page view (`computeThreats`, line 2468); nothing is persisted. There is no "new since yesterday", no acknowledge/mute, and email delivery is a stored string waiting for SMTP "once the mail setup is verified" (line 903). The backend `/api/alerts` feed exists but the rich threat engine doesn't run there.

**The impact.** Threats are only seen when someone opens the dashboard — the tool can't tap anyone on the shoulder. Two people triage the same alerts independently; a muted false positive returns every visit; there's no record of when an undercut started (which §3.1's enforcement case needs).

**The solution.**
1. Port `computeThreats`/`computeLaundryThreats` to the backend, run after each scrape, and persist events (`first_seen, last_seen, resolved_at, state`).
2. Ship a **daily digest** — email (finish the SMTP setup) or a Slack/Teams webhook, which is usually a faster win: new disruptions, new MAP violations (§3.1), resolved threats, biggest movers.
3. Add acknowledge/mute per alert in the UI, persisted server-side, so triage is done once, by one team.
4. Keep the client engine temporarily as a preview ("what-if" with unsaved SKU stars) — but the system of record moves server-side.

### 3.4 History is collected but trends are never summarized

**The problem.** Price history powers a manual chart (pick a model, look) and the Deal Radar's last-two-points delta. There is no aggregate trend anywhere: no brand price index over time, no promo-depth tracking, no "market average moved -2.1% this month".

**The impact.** The team can answer "what is the price now?" but not "is Beko getting structurally cheaper vs Samsung this quarter?" — the question brand management actually asks. The data is already in the database; only the aggregation is missing.

**The solution.**
1. From the daily aggregates (1.6), compute a **brand price index** (average price of a fixed matched basket, indexed to 100 at period start) and chart it on My Brands — one line per brand vs market.
2. Track **promo pressure**: share of SKUs with an active was-price discount per brand/retailer per week (the `old_price` data again) — this exposes who is buying share with promos.
3. Add a "vs last week / last month" delta to the dashboard stat cards; the daily table makes this a trivial query.

---

## Pillar 4 — Usability & UI/UX

*Credit where due: the UI is well above internal-tool standard — coherent design system, dark mode, reduced-motion support, mobile drawer/rail collapse, consistent empty states, XSS-escaped rendering, and honest confidence chips. The findings below are workflow gaps, not visual ones.*

### 4.1 No URL state — nothing is shareable or survives refresh

**The problem.** Navigation is `showTab()` class-toggling (line 1357); no hash/route is written. Only theme, starred SKUs and one category filter persist (localStorage). Every carefully-built filter set — a Segments slice, a Compare view proving a parity break — dies on refresh and cannot be sent to a colleague.

**The impact.** For a team tool, "send me the link" is the primary collaboration path. Right now the only way to share a finding is a screenshot, and re-finding a view after refresh means rebuilding filters from memory (the 2-minute auto-refresh raises the stakes — see 4.5).

**The solution.** Serialize `{dept, tab, active filter object}` into `location.hash` on change; parse on load. All filter state already lives in plain serializable objects (`dashFilter`, `expFilters`, `segFilter`, `laundryFilter`…), so this is a contained change with outsized payoff.

### 4.2 Exports ignore the filters people work in — and skip laundry and alerts entirely

**The problem.** The Export page offers three global dumps. The match-matrix CSV is cooling-only (`buildMatches(state.products)`, line 3315); there is no laundry export, no alerts/threats export, and no way to export the *currently filtered* Compare/Segments/Brands view. CSV/Excel history exports are raw backend data (with the glitches of 1.3) opened straight from the API (line 3312).

**The impact.** The workflow every team actually has — filter to a slice, take it to Excel/a meeting — requires re-creating the filter logic manually in the spreadsheet. Alert triage can't be handed to anyone as a file.

**The solution.**
1. Add an **Export this view** button on Compare, Segments, My Brands and Alerts that serializes the *visible* rows (the render functions already hold them) to CSV client-side — the `exportMatches` blob pattern (line 3314) is reusable as-is.
2. Include per-row: retailer URLs, match confidence, scraped-at, and (post-§3.1) SRP index — the columns a pricing analyst needs to trust a row.
3. Add laundry to the global exports; route them through the authenticated API (§0).

### 4.3 Global search silently jumps to the cooling Compare page

**The problem.** Typing in the topbar search immediately switches the visible page to `page-compare` and filters there (lines 3094–3099) — bypassing `showTab`, so it ignores the department: in Laundry mode you're teleported to the *cooling* explorer, while the dept toggle still says Laundry and the nav state desyncs.

**The impact.** Search — the most-used control on any dashboard — behaves unpredictably for half the catalogue and can strand the UI in an inconsistent state.

**The solution.** Route search through `showTab("compare")` so the dept mapping applies, and feed `lexpFilters.q` when `state.dept === "laundry"`. Better: debounce and show a small dropdown of matches (both departments) instead of hijacking navigation on the first keystroke.

### 4.4 The Products table renders unbounded

**The problem.** Every other list caps rendering (150/200/400 rows) — but `renderProducts` maps over *all* of `state.products` with no slice (line 2333).

**The impact.** At a realistic 1–2k cooling listings, this page becomes the slowest render in the app, re-executed on every `renderAll` (i.e., every 2 minutes and on most interactions).

**The solution.** Apply the same pattern used in `renderLProducts`: sort, slice to ~200, show "N total — search to narrow", and add the search box this page currently lacks.

### 4.5 The 2-minute full refresh re-renders every view and can eat user input

**The problem.** `setInterval(loadAll, 120000)` (line 3391) re-fetches and re-runs `renderAll()`, which rebuilds filter bars and result lists via `innerHTML` — including inputs the user may be interacting with — even though underlying data changes at most a few times a day. Failed background loads of history/alerts are swallowed silently (`.catch(()=>{})`, line 3309), so those panels show "no data yet" indistinguishably from "request failed".

**The impact.** Mid-typing wipes and scroll jumps every two minutes; wasted backend load; and when history fails, Deal Radar quietly shows "no price changes" — a *false negative* dressed as calm.

**The solution.**
1. Gate the interval on the `/api/status` version check from §1.1 — re-render only when data actually changed.
2. Never re-render a page with a focused input; or preserve focus/selection across renders.
3. Distinguish failure from emptiness: on history/alerts fetch failure show a small "couldn't refresh — retrying" chip instead of the empty state.

### 4.6 Code health: duplicates and dead code in a 3,400-line single file

**The problem.** `renderLDashboard` is defined twice back-to-back (lines 1400–1402); `_loadAll_old` is dead (line 1320); config, engine, and views live in one file served with cache-busting disabled and CDN dependencies (Chart.js, Google Fonts).

**The impact.** Low today, compounding monthly: every engine change risks the UI and vice versa; nobody can unit-test `extractModel` or `computeThreats` — the two functions where a silent regression costs real money.

**The solution.** Split into `engine.js` (parsing/normalizing/matching/threats — pure functions), `views.js`, `config.js`; add a handful of unit tests for `extractModel`, `parseDims`, `computeThreats` with real scraped-name fixtures. Vendor Chart.js locally so the tool works if the CDN is blocked. No framework needed.

---

## Pillar 5 — Feature Optimization (underutilized & AI-powered)

### 5.1 Captured data that nothing analyzes yet — three free wins

**The problem.** The scraper already collects fields the analytics never use:
- `old_price` → only strikethrough display (`fmtWas`), never promo analytics (§3.4.2) or promo-vs-structural alert tagging (§3.2.1).
- The per-product **threshold** field on Add Product posts to the backend (line 3298) but no view ever shows threshold breaches.
- `BRAND_SETTINGS` margin/VAT (line 992) — unused pending SRP (§3.1.5).

**The impact.** You've already paid the scraping and storage cost; the insight layer is the only missing piece. These are the cheapest wins in this audit.

**The solution.** Implement §3.2.1 (promo tagging), §3.4.2 (promo-pressure chart), and either surface threshold alerts in the Alerts page or remove the field so the UI doesn't promise what it doesn't deliver.

### 5.2 The confidence system stops one step short of a workflow

**The problem.** Five verdict levels with tooltips (`CONF`, lines 2324–2330) — but no screen lists the `conflict` groups, and no count is surfaced ("14 matches need review").

**The impact.** The most differentiated feature in the tool is invisible unless you hover the right chip.

**The solution.** A "Matches needing review: N" stat card on the dashboard linking to the match-review queue (§2.2). Trivial once 2.2 exists; do them together.

### 5.3 No LLM anywhere in the pipeline — three high-ROI insertion points

**The problem.** As of 2026, LLM-assisted matching and summarization are standard in commercial price-intelligence products, and this codebase has unusually good hooks for both (structured specs per site, confidence verdicts, a movers feed). Currently every ambiguous case falls to heuristics or to nobody.

**The impact.** Match coverage plateaus at what regexes can express; weekly reporting stays manual; spec-poor listings (e.g. Plaisio rows arriving with empty `specs {}`) stay unusable for similarity scoring.

**The solution.** In value order:
1. **Match adjudication** — batch the `conflict`/`codeonly` groups (names + specs + prices from all retailers) to an LLM (e.g. Claude Haiku for cost) asking "same product? (yes/no/variant) + reason". Write results to `match_overrides` as *suggested*; a human confirms in the review UI (§2.2). This typically resolves the long tail heuristics can't, at ~zero marginal engineering after 2.2.
2. **Weekly digest generation** — feed the week's movers, new/resolved threats, MAP violations (§3.1) and index shifts (§3.4) to an LLM to draft the Monday summary email in plain Greek/English. Attach the CSVs from §4.2. This is the feature that makes the platform visible to management.
3. **Spec extraction fallback** — when a PDP yields no structured specs, extract energy class / capacity / dimensions from the scraped description text via LLM into the same canonical fields `normalize()` produces. Fills the holes that currently degrade similarity scoring and confidence checks.

### 5.4 Deal Radar has no business lens

**The problem.** The radar filters only All / Drops / Rises (line 972–976). No "my brands only", no "threats to my SKUs", no department/segment scope, and it inherits the >35% blind spot (§1.4).

**The impact.** The most glanceable surface in the app answers "what moved?" but not "what moved *against me*?" — so users still have to cross-reference Alerts manually.

**The solution.** Add two chips: **My brands** (reuse `isMyBrand`) and **Affects my SKUs** (movers that appear as rivals in the current threat set), plus the verification section from §1.4. The mover objects already carry name/site; this is presentation-layer work.

---

## Suggested sequencing (90 days)

**Weeks 1–2 — Trust & safety:** API auth (§0) · scheduled scraping + status endpoint + freshness badge (§1.1–1.2) · availability "Unknown" bucket (§1.5) · Products table cap (§4.4).

**Weeks 3–6 — The money layer:** SRP/MAP import + violation flags + SRP-index columns (§3.1) · server-side alert persistence + daily Slack/email digest (§3.3) · promo-vs-structural tagging (§3.2.1).

**Weeks 7–10 — Matching you can defend:** EAN/MPN scraping + server-side match table (§2.1, 2.3) · match-review UI + overrides (§2.2, 5.2) · LLM adjudication feeding the queue (§5.3.1) · fix `rivalModelKey` and laundry-type fallback (§2.4–2.5).

**Weeks 11–13 — Workflow polish:** filter-aware exports incl. laundry & alerts (§4.2) · URL state / shareable links (§4.1) · brand price index + promo-pressure charts (§3.4) · Deal Radar business filters + verification list (§5.4, 1.4) · LLM weekly digest (§5.3.2).

---

## Addendum (2026-07-07): backend review — corrections & new findings

After the initial audit, the backend source was provided (`main.py`, `database.py`, `exports.py`, `local_runner.py`, `plp_scraper.py`). The following corrects and extends the findings above. Items marked **[fixed]** were implemented on this branch.

### Corrections to the original audit

- **§0 (auth), refined.** The ingest path (`POST /api/ingest/results`) *is* protected by an `X-Ingest-Token` check — good. Everything else (products read/write/delete, history, exports, scrape triggers) was open, with CORS `allow_origins=["*"]`. **[fixed]** — `main.py` now enforces an `X-API-Key` header on all non-ingest `/api/*` routes when the `API_KEY` env var is set (rollout-safe: unset = open with a startup warning); the dashboard prompts once for the key and stores it.
- **§1.2 (manual collection), refined.** Collection is not purely manual: the backend has an APScheduler cron (09:00/17:00 Athens) that is a **no-op** under `SCRAPE_MODE=local`, and the runner has watchdog/`--due`/`--max-minutes` flags clearly built for a Windows scheduled task. The real gap is that the pipeline's single point of failure is one PC, with no failure notification — if the PC doesn't run, nothing alerts anyone. §1.1/§1.2 recommendations stand with that framing.
- **§1.1 (freshness), easier than stated.** `GET /api/products` already returns `scraped_at` per product — only the UI was missing. **[fixed]** — the topbar now shows per-retailer freshness (green ≤24h / amber ≤48h / red), and a new `GET /api/status` endpoint exposes per-site last-scrape + product counts for external monitoring.
- **§1.3 (ingestion validation), partially in place.** The runner already defends against the worst glitch sources at scrape time: financing-line prices (`best_visible_price`, `_walk_for_price` skips financing subtrees), European/US decimal ambiguity, and a >5× JSON-vs-visible sanity override. The remaining gap from §1.3 stands: no last-known-price comparison, no quarantine table, and exports still ship raw rows.
- **§5.1 (threshold field).** The backend *does* implement threshold alerts (`check_and_alert`, `Alert` table, `email_sent` flag) — the field is wired. `alerts.py` (provided later) confirms full SMTP delivery is implemented and env-var driven: **emails start sending as soon as `SMTP_USER`/`SMTP_PASS` are set on Railway** (Gmail app password works). Remaining gaps: no UI surfaces threshold breaches (the Alerts page only shows the client-side threat engine), and §3.3's competitive-threat digest still has no delivery path — but the SMTP plumbing to reuse for it now demonstrably exists.

### New findings from the backend code

- **Secret in source.** `local_runner.py` had the production `INGEST_TOKEN` hard-coded. **[fixed]** — it now requires the env var; **rotate the token on Railway**, since the old value existed in local copies.
- **Version skew: `/api/ingest/retire`.** The runner calls `POST /api/ingest/retire` after full sweeps, but the provided `main.py` has no such route — either `main.py` is not the latest version, or every full sweep's retire step 404s (logged as non-fatal) and delisted "zombie" products are never retired. **Action:** confirm which is true; commit the real latest `main.py`.
- **Retailer SKU scraped, then thrown away.** The PLP fetchers capture each site's own SKU id (`sku`/`sku_id` — Public/Plaisio ItemList JSON, Kotsovolos `data-cnstrc-item-id`), but `_plp_row_to_result` dropped it and the DB had no column for it. This is the stable per-site identity that §2.1's matching upgrade needs, already available for free. **[fixed]** — the runner now passes `sku` through, `products.retailer_sku` column added (with migration + index), ingest stores/backfills it, and `/api/products` returns it. Next step (§2.1): use it as a match anchor and add EAN capture.
- **N+1 query in `/api/products`.** `list_products` ran one `_latest_history` query per product (≈2,000 queries per request, refreshed every 2 minutes by each open dashboard). **[fixed]** — latest rows are now fetched in a single grouped join.
- **Quadratic CSV export.** `export_price_history_csv` did a linear scan of all products *per history row* (~200M comparisons at 100k rows × 2k products). **[fixed]** — dict lookup, matching the Excel exporter.
- **`/api/history` had no time filter.** Only a row-count `limit` (frontend asks for 20,000 newest rows), so growth silently truncates the oldest data (§1.6). **[fixed, partially]** — the endpoint now accepts `?days=N`; the daily-aggregation table from §1.6 is still the right long-term fix.
- **`is_new`/`first_seen` never set by ingest.** The columns exist and the frontend could use them, but auto-registered products didn't populate them. **[fixed]** — set on creation.
- **`plp_scraper.py` contained its entire source twice** (accidental duplication; second copy silently shadowed the first). **[fixed]** — deduplicated in the committed version.
- **DB growth: `raw_data` snapshots.** Every scrape stores the full JSON snapshot per product in `price_history.raw_data`, and `/api/products` parses the latest snapshot per product on every request. Fine today on a Railway volume; worth revisiting alongside the daily-aggregation work (move specs to a `product_specs` table updated on change, keep `raw_data` for audit only).
- **Unauthenticated scrape triggers.** `POST /api/scrape` and `/api/scrape/all` let anyone start Selenium scrape jobs on the server (resource burn). Now covered by the API-key middleware **[fixed]**, but consider removing them entirely while `SCRAPE_MODE=local` makes them unusable anyway.

### Second addendum: PLP fetchers, alerts.py, requirements.txt reviewed

All remaining files except `scrapers.py` were subsequently provided and committed. Findings:

- **The PLP fetchers are the strongest part of the pipeline.** All three capture the retailer's stable `sku_id` on every row (Public/Plaisio JSON `sku`, Kotsovolos `partNumber`), clean canonical sale + list prices (Public's `salePrice` from `priceInfoDto`, Kotsovolos's `Offer`/`Display` usage split — both structurally immune to the financing-price trap), and structured specs where the site exposes them (Public `topSpecs`, Kotsovolos `attributes[]`). The multi-sort union pass in `public_plp.collect_all` (cycling sort orders until `totalCount` is reached) is a genuinely robust answer to keyset-cursor dead-ends.
- **Spec asymmetry confirmed (§5.3.3).** Plaisio's listing has no structured specs — capacity/cooling/energy are text-mined from name/description (plus the landscape-layout card text for laundry). This is exactly where an LLM spec-extraction fallback pays off first.
- **Laundry loader detection is a clever workaround** (crawling front/top filtered listings and tagging by `sku_id`) but doubles the washing-machine crawl; if Public's `sapHierarchy`/`virtualCategories` diagnostic (`dump_sap`) finds the load type inline, prefer that.
- **`alerts.py` is clean**: SMTP creds from env vars only, threshold alerts fully implemented. Delivery activates the moment `SMTP_USER`/`SMTP_PASS` are configured.
- **`requirements.txt` doesn't pin `requests`** (used directly by the runner and two fetchers; currently a transitive dependency) and leaves `apscheduler` unpinned. Add `requests==2.x` and pin `apscheduler` for reproducible installs.
- **Still missing:** `scrapers.py` (imported by `backend/main.py` — `scrape_url`, `scrape_batch`, `detect_site`). The backend cannot start without it; commit it to complete the repo.

---

*Audit based on static review of the uploaded `index.html` and backend/runner sources listed above. Recommendations touching files not provided are framed as contracts to implement rather than diffs.*
